"""Check results are bound to content (lead ruling, contracts 2.5.0), on both backends.

Every row here comes from the real `truth.to_check_results`, bound to the hash the gate computes
for the variant as it is at that moment (`gate.content_hashes`). The ruling replaces a bug: the
compiler writes FAIL and FLAG rows under `truth.compile.<kind>` and a clean rerun writes PASS under
bare `truth.compile`, so "latest attempt per key" alone could never clear a FAIL. With content
binding, corrected content has a new hash, and that is what clears it.

What append-only does and does not prevent (test_a_forged_later_pass_...): the app role cannot
rewrite or delete a FAIL row, so the FAIL stays on record, but it can still insert rows, so a
later PASS under the same key for the same content supersedes the FAIL at the gate. Closing that
needs a writer the app role is not (a signed or separately owned check stack); the database alone
does not.
"""
# ruff: noqa: F811  (the backend, shop and signer fixtures are imported from the wave 1 flow)
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

from thallo.contracts.enums import CheckLayer, CheckVerdict, Language
from thallo.contracts.models import Bundle, CheckResult, Variant
from thallo.contracts.states import BundleState, VariantState
from thallo.gate import ApprovalRefused, BlockReason, check_publish, content_hashes, void_bundle_approvals
from thallo.truth import build, compile_text, library, to_check_results

from .test_wave1_flow import PgBackend, Shop, _id, approve, backend, publish, seed, shop, signer  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- helpers

def set_text(b, shop: Shop, text: str, rendered: str) -> None:
    with b.session(shop.tenant.id) as s:
        s.update(Variant, shop.variant.id, text=text, rendered_text=rendered)


def run_truth(b, shop: Shop, text: str, *, attempt: int = 1) -> list[CheckResult]:
    """The real truth compile on `text`, written through the real `to_check_results`, bound to the
    variant's hash as it is now. Returns the rows written."""
    t = shop.tenant.id
    with b.session(t) as s:
        compiled = compile_text(text, tenant_id=t, language=Language.IT, claims=library(s.claims, t), clock=b.clock)
        sha = content_hashes(s.gate, t, shop.bundle.id)[shop.variant.id]
        rows = to_check_results(compiled, variant_id=shop.variant.id, content_sha256=sha, clock=b.clock,
                                attempt=attempt)
        for r in rows:
            s.insert(r)
    return rows


def back_to_review(b, shop: Shop, signer) -> None:
    t = shop.tenant.id
    with b.session(t) as s:
        s.write(void_bundle_approvals(s.get(Bundle, shop.bundle.id), s.list(Variant, bundle_id=shop.bundle.id),
                                      s.gate.get_approvals(t, shop.bundle.id), reason="text edited",
                                      actor=shop.human, clock=b.clock, audit=s.audit, signer=signer))
        s.update(Bundle, shop.bundle.id, state=BundleState.IN_REVIEW)
        s.update(Variant, shop.variant.id, state=VariantState.REVIEW)


def bad_text(shop: Shop) -> str:
    return shop.variant.text.replace("{{C-0001}}", "130 km")  # a digit outside a slot: BLOCK


def corrected(b, shop: Shop):
    """The flagged "il top" removed: builds clean, no flags."""
    t = shop.tenant.id
    with b.session(t) as s:
        return build(shop.variant.text.replace(", il top", ""), tenant_id=t, language=Language.IT,
                     claims=library(s.claims, t), clock=b.clock, default_language=Language.IT)


class ChecksDown:
    """A repository whose check store fails; everything else reads through."""

    def __init__(self, gate) -> None:
        self._gate = gate

    def __getattr__(self, name):
        return getattr(self._gate, name)

    def get_check_results(self, tenant_id, variant_id, content_sha256):
        raise RuntimeError("check store unavailable")


# ---------------------------------------------------------------- the bug this replaces

def test_correcting_the_text_clears_its_fail_and_restoring_it_brings_the_fail_back(backend, signer):
    shop = seed(backend, "Lambda")
    bad = bad_text(shop)
    set_text(backend, shop, bad, bad)
    failing = run_truth(backend, shop, bad)
    fail_names = {r.check_name for r in failing if r.verdict is CheckVerdict.FAIL}
    assert fail_names and all(n.startswith("truth.compile.") for n in fail_names)
    with pytest.raises(ApprovalRefused, match="FAIL"):
        approve(backend, shop, signer, acknowledged=[r.id for r in failing if r.verdict is CheckVerdict.FLAG])

    fixed = corrected(backend, shop)
    set_text(backend, shop, fixed.compiled.text, fixed.filled.text)
    clean = run_truth(backend, shop, fixed.compiled.text, attempt=2)
    assert [(r.check_name, r.verdict) for r in clean] == [("truth.compile", CheckVerdict.PASS)]
    assert not fail_names & {r.check_name for r in clean}  # a different key: latest-per-key alone never cleared it
    granted = approve(backend, shop, signer, acknowledged=[])
    assert granted.acknowledged_flag_ids == []
    assert publish(backend, shop, signer).allowed

    back_to_review(backend, shop, signer)
    set_text(backend, shop, bad, bad)  # the old content again: its FAIL rows count again
    with pytest.raises(ApprovalRefused, match="FAIL"):
        approve(backend, shop, signer, acknowledged=[r.id for r in failing if r.verdict is CheckVerdict.FLAG])


# ---------------------------------------------------------------- what counts

def test_a_fail_for_the_current_content_refuses(backend, signer):
    shop = seed(backend, "Mu", checks=False)
    bad = bad_text(shop)
    set_text(backend, shop, bad, bad)
    run_truth(backend, shop, bad)
    with pytest.raises(ApprovalRefused, match="FAIL for the current content"):
        approve(backend, shop, signer, acknowledged=[])


def test_a_fail_for_older_content_is_ignored(backend, signer):
    shop = seed(backend, "Nu")
    original = (shop.variant.text, shop.variant.rendered_text)
    bad = bad_text(shop)
    set_text(backend, shop, bad, bad)
    old = run_truth(backend, shop, bad)
    assert any(r.verdict is CheckVerdict.FAIL for r in old)
    set_text(backend, shop, *original)  # back to the seeded content; its rows were written by seed
    granted = approve(backend, shop, signer)
    assert granted.acknowledged_flag_ids == shop.flag_ids  # the old rows' FLAG is not asked for either


def test_no_check_rows_refuses_as_not_checked(backend, signer):
    shop = seed(backend, "Xi", checks=False)
    with pytest.raises(ApprovalRefused, match="not checked"):
        approve(backend, shop, signer, acknowledged=[])
    with backend.session(shop.tenant.id) as s:
        assert s.get(Bundle, shop.bundle.id).state is BundleState.IN_REVIEW


def test_only_non_code_rows_refuses_as_not_checked(backend, signer):
    shop = seed(backend, "Omicron", checks=False)
    t = shop.tenant.id
    fixed = corrected(backend, shop)
    set_text(backend, shop, fixed.compiled.text, fixed.filled.text)
    with backend.session(t) as s:
        sha = content_hashes(s.gate, t, shop.bundle.id)[shop.variant.id]
        [row] = to_check_results(fixed.compiled, variant_id=shop.variant.id, content_sha256=sha, clock=backend.clock)
        for layer in (CheckLayer.GRADER, CheckLayer.SECOND_READER):  # right content, wrong layer
            s.insert(row.model_copy(update={"id": _id(backend), "layer": layer}))
    with pytest.raises(ApprovalRefused, match="not checked"):
        approve(backend, shop, signer, acknowledged=[])


def test_an_unacknowledged_flag_for_the_current_content_refuses(backend, signer):
    shop = seed(backend, "Pi")
    with pytest.raises(ApprovalRefused, match="1 open flag"):
        approve(backend, shop, signer, acknowledged=[])
    assert approve(backend, shop, signer).acknowledged_flag_ids == shop.flag_ids


def test_a_forged_later_pass_for_the_same_content_is_not_prevented_but_the_fail_stays_on_record(backend, signer):
    """Documents the limit of append-only: an app-role writer can insert a later PASS under the
    FAIL's own key for the same content, and the gate takes the highest attempt. The FAIL row itself
    cannot be rewritten or deleted (Postgres test below), so the record of it survives."""
    shop = seed(backend, "Rho", checks=False)
    bad = bad_text(shop)
    set_text(backend, shop, bad, bad)
    rows = run_truth(backend, shop, bad)
    fails = [r for r in rows if r.verdict is CheckVerdict.FAIL]
    flags = [r.id for r in rows if r.verdict is CheckVerdict.FLAG]
    with pytest.raises(ApprovalRefused, match="FAIL"):
        approve(backend, shop, signer, acknowledged=flags)
    t = shop.tenant.id
    with backend.session(t) as s:
        for f in fails:
            s.insert(f.model_copy(update={"id": _id(backend), "verdict": CheckVerdict.PASS, "attempt": 2,
                                          "reason": "forged"}))
    approve(backend, shop, signer, acknowledged=flags)  # NOT prevented: the forged PASS supersedes
    with backend.session(t) as s:
        assert all(s.get(CheckResult, f.id) == f for f in fails)  # the FAIL rows are unchanged


# ---------------------------------------------------------------- publish re-reads the checks

def test_publish_blocks_on_a_fail_for_the_content_being_sent(backend, shop, signer):
    assert publish(backend, shop, signer).allowed
    t = shop.tenant.id
    with backend.session(t) as s:  # the check stack reruns on the same content and now fails
        seeded = s.get(CheckResult, shop.flag_ids[0])
        bad = compile_text(bad_text(shop), tenant_id=t, language=Language.IT, claims=library(s.claims, t),
                           clock=backend.clock)
        for r in to_check_results(bad, variant_id=shop.variant.id, content_sha256=seeded.content_sha256,
                                  clock=backend.clock, attempt=2):
            s.insert(r)
    decision = publish(backend, shop, signer)
    assert not decision.allowed and BlockReason.CHECK_FAILED in decision.reasons
    assert decision.payload is None


def test_publish_blocks_when_the_content_being_sent_was_never_checked(backend, shop, signer):
    with backend.session(shop.tenant.id) as s:
        s.update(Variant, shop.variant.id, rendered_text=shop.variant.rendered_text.replace("Fino", "Oltre"))
    decision = publish(backend, shop, signer)
    assert not decision.allowed
    assert {BlockReason.NOT_CHECKED, BlockReason.CONTENT_CHANGED} <= set(decision.reasons)


def test_publish_blocks_when_the_check_results_cannot_be_read(backend, shop, signer):
    t = shop.tenant.id
    backend.clock.advance(seconds=5)
    with backend.session(t) as s:
        decision = check_publish(ChecksDown(s.gate), tenant_id=t, bundle_id=shop.bundle.id,
                                 variant_id=shop.variant.id, approval_id=shop.approval.id, clock=backend.clock,
                                 signer=signer)
    assert not decision.allowed and BlockReason.CHECKS_UNREADABLE in decision.reasons
    assert "check store unavailable" in decision.blocks[decision.reasons.index(BlockReason.CHECKS_UNREADABLE)].detail
    assert publish(backend, shop, signer).allowed


# ---------------------------------------------------------------- Postgres: append-only and bounds

def _pg(backend):
    if not isinstance(backend, PgBackend):
        pytest.skip("database privileges: Postgres only")
    import psycopg
    return psycopg


def test_postgres_app_role_cannot_update_or_delete_check_results(backend, shop):
    psycopg = _pg(backend)
    row = shop.flag_ids[0]
    for statement in ("UPDATE check_result SET verdict = 'pass' WHERE id = %s",
                      "DELETE FROM check_result WHERE id = %s"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), backend.session(shop.tenant.id) as s:
            s.repo.execute(statement, (row,))
    with backend.session(shop.tenant.id) as s:
        assert s.get(CheckResult, row).verdict is CheckVerdict.FLAG


def test_postgres_append_only_holds_even_if_a_grant_comes_back(backend, shop):
    psycopg = _pg(backend)
    row = shop.flag_ids[0]
    admin = backend.urls["admin"]
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("GRANT UPDATE, DELETE ON check_result TO cortex_app")
    try:
        for statement in ("UPDATE check_result SET verdict = 'pass' WHERE id = %s",
                          "DELETE FROM check_result WHERE id = %s"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"), \
                    backend.session(shop.tenant.id) as s:
                s.repo.execute(statement, (row,))
        with psycopg.connect(admin) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE check_result SET verdict = 'pass' WHERE id = %s", (row,))  # the owner too
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("REVOKE UPDATE, DELETE ON check_result FROM cortex_app")
    with backend.session(shop.tenant.id) as s:
        assert s.get(CheckResult, row).verdict is CheckVerdict.FLAG


def test_postgres_new_rows_need_a_real_hash_and_an_attempt_of_one_or_more(backend, shop):
    psycopg = _pg(backend)
    t = shop.tenant.id
    with backend.session(t) as s:
        seeded = s.get(CheckResult, shop.flag_ids[0])
    insert = ("INSERT INTO check_result (id, created_at, tenant_id, variant_id, layer, check_name, verdict, attempt, "
              "content_sha256) VALUES (%s, %s, %s, %s, 'code', 'x', 'pass', %s, %s)")
    for attempt, sha in ((0, seeded.content_sha256), (1, "unbound-before-0021"), (1, "A" * 64)):
        with pytest.raises(psycopg.errors.CheckViolation), backend.session(t) as s:
            s.repo.execute(insert, (_id(backend), backend.clock.now(), t, shop.variant.id, attempt, sha))


def test_postgres_migration_marks_existing_rows_so_they_never_count(tmp_path):
    """Rows written before 0021 get the marker, which never equals a real hash. They stay, inert:
    the NOT VALID checks spare them, and nothing can update them into counting rows."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL unset: runs under scripts/ci.sh")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import MIGRATIONS, migrate

    before = tmp_path / "before"
    before.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < "0021":
            (before / path.name).write_text(path.read_text())
    name = f"cortex_mig_{secrets.token_hex(4)}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(url)
    params["dbname"] = name
    db = make_conninfo(**params)
    try:
        migrate(db, before)
        with psycopg.connect(db) as conn:  # the throwaway server's superuser: no RLS, no foreign keys
            conn.execute("SET session_replication_role = replica")
            conn.execute("INSERT INTO check_result (id, created_at, tenant_id, variant_id, layer, check_name, "
                         "verdict, attempt) VALUES (gen_random_uuid(), now(), gen_random_uuid(), gen_random_uuid(), "
                         "'code', 'truth.compile.digit', 'fail', 1)")
        assert migrate(db) == ["0021_check_content_hash.sql"]
        with psycopg.connect(db) as conn:
            assert conn.execute("SELECT content_sha256 FROM check_result").fetchall() == [("unbound-before-0021",)]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE check_result SET content_sha256 = %s", ("a" * 64,))
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
