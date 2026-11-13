"""The approval gate never decides on a partial read. Adapters read every row in pages
(`TenantRepo.list_all`, `query_all`, `fetch_all`); past a hard ceiling they raise, and
`check_publish` turns that raise into a block. Page size and ceiling are shrunk here so a handful
of rows crosses them."""
from __future__ import annotations

import pytest

from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import Variant
from cortex_operator.contracts.states import VariantState
from cortex_operator.gate import BlockReason
from cortex_operator.store import db as store_db
from cortex_operator.store.adapters import LocalBlobStore
from cortex_operator.store.db import TooManyRows

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


def test_a_bundle_with_more_variants_than_a_page_is_read_whole(backend, monkeypatch):
    shop = seed(backend, "PageA")
    t = shop.tenant.id
    _more_variants(backend, shop, 4)  # 5 variants in all
    monkeypatch.setattr(store_db, "PAGE_SIZE", 2)
    with backend.session(t) as s:
        got = s.gate.get_variants(t, shop.bundle.id)
        assert len(got) == 5 and len({v.id for v in got}) == 5
        assert [v.id for v in got] == sorted(v.id for v in got)
        assert len(s.repo.list_all(Variant)) == 5
        assert s.repo.list_all(Variant, bundle_id=new_id(backend.clock)) == []


def test_an_audit_chain_longer_than_a_page_verifies(backend, signer, monkeypatch):  # noqa: F811
    shop = seed(backend, "PageB")
    t = shop.tenant.id
    approve(backend, shop, signer)
    with backend.session(t) as s:
        for i in range(6):
            s.audit.append(t, shop.human, "test.note", subject_kind="note", subject_id=shop.bundle.id,
                           payload={"i": i})
    monkeypatch.setattr(store_db, "PAGE_SIZE", 2)
    with backend.session(t) as s:
        entries = s.audit.store.entries(t)
        assert [e.seq for e in entries] == list(range(1, 8))
        assert s.audit.verify(t).ok
        assert [e.seq for e in s.gate.get_audit_chain(t, 4)] == [4, 5, 6, 7]
        assert len(s.gate.get_audit_entries(t, "note", shop.bundle.id)) == 6


def test_over_the_ceiling_raises_and_publish_blocks(backend, signer, monkeypatch):  # noqa: F811
    shop = seed(backend, "PageC")
    t = shop.tenant.id
    approve(backend, shop, signer)
    monkeypatch.setattr(store_db, "PAGE_SIZE", 2)
    monkeypatch.setattr(store_db, "MAX_ROWS", 3)
    assert publish(backend, shop, signer).allowed  # below the ceiling nothing changes
    _more_variants(backend, shop, 4)  # 5 variants, ceiling 3
    with backend.session(t) as s:
        with pytest.raises(TooManyRows):
            s.gate.get_variants(t, shop.bundle.id)
        with pytest.raises(TooManyRows):
            s.repo.list_all(Variant)
        with pytest.raises(TooManyRows):
            s.repo.query_all(Variant, "SELECT * FROM variant ORDER BY id")
    decision = publish(backend, shop, signer)
    assert not decision.allowed and decision.reasons == (BlockReason.REPO_ERROR,)
    assert "TooManyRows" in decision.blocks[0].detail


def test_exactly_at_the_ceiling_is_allowed(backend, monkeypatch):
    shop = seed(backend, "PageD")
    t = shop.tenant.id
    _more_variants(backend, shop, 2)  # 3 variants
    monkeypatch.setattr(store_db, "PAGE_SIZE", 2)
    monkeypatch.setattr(store_db, "MAX_ROWS", 3)
    with backend.session(t) as s:
        assert len(s.gate.get_variants(t, shop.bundle.id)) == 3
