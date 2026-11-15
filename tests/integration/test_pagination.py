"""The approval gate never decides on a partial or inconsistent read.

General reads (`TenantRepo.list_all`, `query_all`, `fetch_all`) are ONE statement each, capped at
`MAX_ROWS + 1` rows: one statement is one snapshot, and past the cap they raise `TooManyRows`
instead of returning a prefix. `check_publish` turns that raise into a block.

The append-only audit chain has its own, much larger ceiling (`AUDIT_MAX_ROWS`) and is streamed
through a server-side (named) cursor, so a long-lived tenant does not hit the general cap."""
from __future__ import annotations

import pytest

from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.ids import new_id
from thallo.contracts.models import CheckResult, Variant
from thallo.contracts.states import VariantState
from thallo.gate import BlockReason
from thallo.store import db as store_db
from thallo.store.adapters import LocalBlobStore
from thallo.store.db import TooManyRows

from .backends import PgBackend
from .test_wave1_flow import approve, publish, seed, signer  # noqa: F401  (signer is a fixture)


@pytest.fixture
def backend(pg_urls, tmp_path):
    return PgBackend(pg_urls, FakeClock(FAKE_EPOCH), LocalBlobStore(tmp_path / "blobs"))


def _more_variants(backend, shop, n):
    with backend.session(shop.tenant.id) as s:
        for _ in range(n):
            backend.clock.advance(seconds=1)
            s.insert(shop.variant.model_copy(update={"id": new_id(backend.clock), "state": VariantState.REVIEW}))


def _check_rows(backend, shop) -> int:
    """How many check rows the seeded variant has (truth keys plus design lint): publish reads them
    all in one statement, so the lowered ceiling must sit at or above this count."""
    with backend.session(shop.tenant.id) as s:
        return len(s.repo.list_all(CheckResult, variant_id=shop.variant.id))


def _more_audit(backend, shop, n):
    t = shop.tenant.id
    with backend.session(t) as s:
        for i in range(n):
            s.audit.append(t, shop.human, "test.note", subject_kind="note", subject_id=shop.bundle.id,
                           payload={"i": i})


class _Cursor:
    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def execute(self, query, *a, **k):
        self._log.append(("execute", query if isinstance(query, str) else query.as_string(None)))
        return self._inner.execute(query, *a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class _Conn:
    """Passes everything to the real connection and records statements and named cursors."""

    def __init__(self, inner):
        self._inner = inner
        self.log: list[tuple[str, str]] = []

    def cursor(self, name="", *a, **k):
        if name:
            self.log.append(("named", name))
        return _Cursor(self._inner.cursor(name, *a, **k) if name else self._inner.cursor(*a, **k), self.log)

    def execute(self, query, *a, **k):
        self.log.append(("execute", query if isinstance(query, str) else query.as_string(None)))
        return self._inner.execute(query, *a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _spy(s) -> _Conn:
    spy = _Conn(s.repo.conn)
    s.repo.conn = spy
    return spy


def test_each_general_read_is_one_statement(backend, monkeypatch):
    monkeypatch.setattr(store_db, "PAGE_SIZE", 2, raising=False)  # a leftover paging knob must change nothing
    shop = seed(backend, "PageA")
    t = shop.tenant.id
    _more_variants(backend, shop, 4)  # 5 variants
    with backend.session(t) as s:
        spy = _spy(s)
        assert len(s.repo.list_all(Variant, bundle_id=shop.bundle.id)) == 5
        assert len(s.repo.query_all(Variant, "SELECT * FROM variant ORDER BY id")) == 5
        assert len(s.repo.fetch_all("SELECT id FROM variant ORDER BY id")) == 5
        selects = [q for kind, q in spy.log if kind == "execute"]
        assert len(selects) == 3  # no second page, no count query
        ids = [v.id for v in s.gate.get_variants(t, shop.bundle.id)]
        assert ids == sorted(ids) and len(ids) == 5
        assert s.repo.list_all(Variant, bundle_id=new_id(backend.clock)) == []


def test_over_the_ceiling_raises_and_publish_blocks(backend, signer, monkeypatch):  # noqa: F811
    shop = seed(backend, "PageC")
    t = shop.tenant.id
    approve(backend, shop, signer)
    ceiling = _check_rows(backend, shop)
    monkeypatch.setattr(store_db, "MAX_ROWS", ceiling)
    assert publish(backend, shop, signer).allowed  # at or below the ceiling nothing changes
    _more_variants(backend, shop, ceiling)  # one variant more than the ceiling
    with backend.session(t) as s:
        with pytest.raises(TooManyRows):
            s.gate.get_variants(t, shop.bundle.id)
        with pytest.raises(TooManyRows):
            s.repo.list_all(Variant)
        with pytest.raises(TooManyRows):
            s.repo.query_all(Variant, "SELECT * FROM variant ORDER BY id")
        with pytest.raises(TooManyRows):
            s.repo.fetch_all("SELECT id FROM variant ORDER BY id")
    decision = publish(backend, shop, signer)
    assert not decision.allowed and decision.reasons == (BlockReason.REPO_ERROR,)
    assert "TooManyRows" in decision.blocks[0].detail


def test_exactly_at_the_ceiling_is_allowed(backend, monkeypatch):
    shop = seed(backend, "PageD")
    t = shop.tenant.id
    _more_variants(backend, shop, 2)  # 3 variants
    monkeypatch.setattr(store_db, "MAX_ROWS", 3)
    with backend.session(t) as s:
        assert len(s.gate.get_variants(t, shop.bundle.id)) == 3
        assert len(s.repo.list_all(Variant)) == 3


# ---------------------------------------------------------------- the audit chain

def test_an_audit_chain_longer_than_the_general_ceiling_verifies_and_publishes(backend, signer, monkeypatch):  # noqa: F811
    shop = seed(backend, "PageB")
    t = shop.tenant.id
    approve(backend, shop, signer)
    ceiling = _check_rows(backend, shop)
    total = ceiling + 4  # longer than the general ceiling
    _more_audit(backend, shop, total - 1)  # the grant is entry 1
    monkeypatch.setattr(store_db, "MAX_ROWS", ceiling)
    monkeypatch.setattr(store_db, "STREAM_BATCH", 2)
    with backend.session(t) as s:
        entries = s.audit.store.entries(t)
        assert [e.seq for e in entries] == list(range(1, total + 1))
        assert s.audit.verify(t).ok
        assert [e.seq for e in s.gate.get_audit_chain(t, 4)] == list(range(4, total + 1))
    decision = publish(backend, shop, signer)
    assert decision.allowed, decision.blocks


def test_the_chain_is_read_through_a_named_cursor(backend, signer):  # noqa: F811
    shop = seed(backend, "PageE")
    t = shop.tenant.id
    approve(backend, shop, signer)
    _more_audit(backend, shop, 3)
    with backend.session(t) as s:
        spy = _spy(s)
        assert len(s.gate.get_audit_chain(t, 1)) == 4
        assert any(kind == "named" for kind, _ in spy.log)


def test_the_audit_chain_has_its_own_ceiling_and_publish_fails_closed(backend, signer, monkeypatch):  # noqa: F811
    shop = seed(backend, "PageF")
    t = shop.tenant.id
    approve(backend, shop, signer)
    _more_audit(backend, shop, 6)  # 7 entries
    monkeypatch.setattr(store_db, "AUDIT_MAX_ROWS", 5)
    with backend.session(t) as s:
        with pytest.raises(TooManyRows):
            s.gate.get_audit_chain(t, 1)
        with pytest.raises(TooManyRows):
            s.audit.store.entries(t)
        assert len(s.gate.get_audit_chain(t, 3)) == 5  # exactly at the ceiling is fine
    decision = publish(backend, shop, signer)
    assert not decision.allowed and decision.reasons == (BlockReason.REPO_ERROR,)
    assert "TooManyRows" in decision.blocks[0].detail
