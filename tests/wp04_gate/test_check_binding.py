"""Check results bound to content, on the in-memory world: cases a real database cannot reach
(rows vanishing, a repository that returns too much) and the pure `decide` contract. Rows come
from the real truth compile through `World.code_check`."""
from __future__ import annotations

import pytest

from tests.wp04_gate.conftest import ident
from thallo.contracts.enums import CheckVerdict
from thallo.gate import ApprovalRefused, BlockReason, check_publish, decide, load_bundle
from thallo.gate.decision import ApprovalEvidence


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
