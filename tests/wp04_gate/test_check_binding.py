"""Check results bound to content, on the in-memory world: cases a real database cannot reach
(rows vanishing, a repository that returns too much) and the pure `decide` contract. Rows come
from the real truth compile through `World.code_check`."""
from __future__ import annotations

import pytest

from tests.wp04_gate.conftest import ident
from thallo.contracts.enums import CheckVerdict, Language
from thallo.gate import ApprovalRefused, BlockReason, ContentType, check_publish, decide, load_bundle, review_checks
from thallo.gate.decision import ApprovalEvidence
from thallo.truth import compile_text, to_check_results


def vid0(w):
    return next(iter(w.variants))


def publish(w, approval_id):
    return check_publish(w, tenant_id=w.tenant.id, bundle_id=w.bundle.id, variant_id=vid0(w),
                         approval_id=approval_id, clock=w.clock, signer=w.signer)


def test_no_rows_refuses_the_grant(world):
    with pytest.raises(ApprovalRefused, match="2 variant"):
        world.approve(run_checks=False)


def test_one_unchecked_variant_is_enough_to_refuse(world):
    world.code_check([vid0(world)])
    with pytest.raises(ApprovalRefused, match="1 variant"):
        world.approve(run_checks=False)


def test_rows_of_another_tenant_or_with_a_stale_hash_never_count(world):
    rows = world.code_check()
    world.checks = [r.model_copy(update={"tenant_id": world.bundle.id}) for r in rows]  # repository leaks
    with pytest.raises(ApprovalRefused, match="not checked"):
        world.approve(run_checks=False)
    world.checks = rows
    world.edit_variant(vid0(world), text="Fino a {{C-0012}} con due cariche.")
    with pytest.raises(ApprovalRefused, match="1 variant"):  # the edited variant's rows are stale
        world.approve(run_checks=False)


def test_publish_blocks_when_the_rows_for_the_sent_content_are_gone(world):
    a = world.approve()
    assert publish(world, a.id).allowed
    world.checks.clear()
    d = publish(world, a.id)
    assert not d.allowed and d.reasons == (BlockReason.NOT_CHECKED,)


def test_publish_blocks_on_a_fail_written_after_the_grant(world):
    a = world.approve()
    rows = [r for r in world.checks if r.variant_id == vid0(world)]
    world.checks += [rows[0].model_copy(update={"id": ident(world.clock), "verdict": CheckVerdict.FAIL,
                                                "attempt": rows[0].attempt + 1})]
    d = publish(world, a.id)
    assert not d.allowed and d.reasons == (BlockReason.CHECK_FAILED,)


def test_publish_blocks_when_the_check_store_fails(world):
    a = world.approve()
    world.fail_on = "get_check_results"
    d = publish(world, a.id)
    assert not d.allowed and d.reasons == (BlockReason.CHECKS_UNREADABLE,)
    assert d.payload is None


def test_decide_without_a_check_reader_blocks(world):
    a = world.approve()
    content = load_bundle(world, world.tenant.id, world.bundle)
    ev = ApprovalEvidence(approval=a, approver=world.users[world.user_id],
                          audit_entries=tuple(world.get_audit_entries(world.tenant.id, "approval", a.id)),
                          chain=tuple(world.get_audit_chain(world.tenant.id, 1)))
    kw = dict(tenant=world.tenant, variant_id=vid0(world), now=world.clock.now(), system_paused=False,
              resolver=world.links.get, signer=world.signer)
    d = decide(content, ev, **kw)
    assert not d.allowed and d.reasons == (BlockReason.CHECKS_UNREADABLE,)
    assert decide(content, ev, check_reader=world.get_check_results, **kw).allowed


def test_review_checks_filters_hashes_itself(world):
    """Ruling 4: rows with mixed hashes go straight to `review_checks`, no repository in between, so
    only the gate's own filter keeps stale rows out. Removing that filter fails every assert here."""
    a, b = list(world.variants)
    t = world.tenant.id
    now_a, now_b, old = world.current_hash(a), world.current_hash(b), "e" * 64
    bad = compile_text("Fino a 130 km.", tenant_id=t, language=Language.IT, claims=list(world.claims.values()),
                       clock=world.clock)  # a digit outside a slot: FAIL
    clean = compile_text("Calme settimane.", tenant_id=t, language=Language.IT, claims=list(world.claims.values()),
                         clock=world.clock)

    def rows(result, vid, sha, attempt):
        return to_check_results(result, variant_id=vid, content_sha256=sha, clock=world.clock, attempt=attempt)

    current_fail = rows(bad, a, now_a, 1)  # the current content fails at attempt 1
    stale_pass = rows(clean, a, old, 2)  # a later PASS under the same keys, but about other content
    stale_fail = rows(bad, b, old, 1)  # a FAIL about b's old content
    stale_only = rows(clean, b, old, 2)  # b has nothing for its current content
    foreign = [r.model_copy(update={"id": ident(world.clock), "tenant_id": world.bundle.id})
               for r in rows(clean, b, now_b, 3)]  # right hash, another tenant
    mixed = [*current_fail, *stale_pass, *stale_fail, *stale_only, *foreign]
    types = {a: {ContentType.TEXT}, b: {ContentType.TEXT}}
    review = review_checks(mixed, tenant_id=t, hashes={a: now_a, b: now_b}, types=types)
    assert review.failing == {r.id for r in current_fail if r.verdict is CheckVerdict.FAIL}
    assert review.unchecked == (b,) and review.missing[b] == (ContentType.TEXT,)
    assert {r.content_sha256 for r in review.counted} == {now_a}
    assert all(r.tenant_id == t for r in review.counted)


def test_publish_applies_the_same_coverage_rule(world):
    """Ruling 1 in `check_publish`: a variant with media and only truth rows is not checked; with
    only lint rows its text is not checked."""
    vid = next(v for v in world.variants if world.variant_media.get(v))
    a = world.approve()
    rows = list(world.checks)
    world.checks = [r for r in rows if not (r.variant_id == vid and r.check_name.startswith("design."))]
    d = check_publish(world, tenant_id=world.tenant.id, bundle_id=world.bundle.id, variant_id=vid,
                      approval_id=a.id, clock=world.clock, signer=world.signer)
    assert not d.allowed and d.reasons == (BlockReason.NOT_CHECKED,) and "media" in d.blocks[0].detail
    world.checks = [r for r in rows if not (r.variant_id == vid and r.check_name.startswith("truth."))]
    d = check_publish(world, tenant_id=world.tenant.id, bundle_id=world.bundle.id, variant_id=vid,
                      approval_id=a.id, clock=world.clock, signer=world.signer)
    assert not d.allowed and d.reasons == (BlockReason.NOT_CHECKED,) and "text" in d.blocks[0].detail
    world.checks = rows
    d = check_publish(world, tenant_id=world.tenant.id, bundle_id=world.bundle.id, variant_id=vid,
                      approval_id=a.id, clock=world.clock, signer=world.signer)
    assert d.allowed
