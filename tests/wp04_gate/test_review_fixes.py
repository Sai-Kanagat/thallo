"""Security review of WP-04: one or more tests per finding, written before the fix."""
from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from cortex_operator.contracts.enums import ApprovalMethod, UserRole
from cortex_operator.contracts.states import BundleState, VariantState
from cortex_operator.gate import (
    MAX_APPROVAL_TTL,
    ApprovalRefused,
    BlockReason,
    PublishPayload,
    approve_bundle_from_repo,
    bundle_hash,
    check_publish,
    void_bundle_approvals,
)
from cortex_operator.gate.canonical import find_links
from cortex_operator.trust.audit import AuditError
from tests.wp04_gate.conftest import ident, make_world


def vid0(w):
    return next(iter(w.variants))


def publish(w, approval_id, variant_id=None, **kw):
    return check_publish(w, tenant_id=w.tenant.id, bundle_id=w.bundle.id, variant_id=variant_id or vid0(w),
                         approval_id=approval_id, clock=w.clock, signer=w.signer, **kw)


def blocked(d, reason):
    assert not d.allowed and reason in d.reasons, d.blocks
    assert d.payload is None


# ---------------------------------------------------------------- 1. a forged approval row

def test_forged_row_with_a_ten_year_expiry_is_blocked(world):
    a = world.approve()
    world.approvals[a.id] = a.model_copy(update={"expires_at": a.approved_at + timedelta(days=3650)})
    blocked(publish(world, a.id), BlockReason.APPROVAL_TTL_INVALID)


def test_ttl_at_the_maximum_is_fine(world):
    a = world.approve(ttl=MAX_APPROVAL_TTL)
    assert publish(world, a.id).allowed


def test_forged_row_with_a_fake_approver_is_blocked(world):
    a = world.approve()
    world.approvals[a.id] = a.model_copy(update={"approver_id": ident(world.clock)})
    d = publish(world, a.id)
    blocked(d, BlockReason.APPROVER_NOT_ALLOWED)
    assert BlockReason.NO_GRANT_RECORD in d.reasons


@pytest.mark.parametrize("role", [UserRole.SELLER, UserRole.STAFF])
def test_approver_without_an_approving_role_is_blocked(world, role):
    a = world.approve()
    world.users[world.user_id] = world.users[world.user_id].model_copy(update={"role": role})
    blocked(publish(world, a.id), BlockReason.APPROVER_NOT_ALLOWED)


def test_approver_from_another_tenant_is_blocked(world):
    a = world.approve()
    world.users[world.user_id] = world.users[world.user_id].model_copy(update={"tenant_id": make_world().tenant.id})
    blocked(publish(world, a.id), BlockReason.APPROVER_NOT_ALLOWED)


def test_row_written_straight_to_storage_without_a_grant_entry_is_blocked(world):
    a = world.approve()
    world.audit_store._rows.clear()  # the row exists, the audited grant does not
    blocked(publish(world, a.id), BlockReason.NO_GRANT_RECORD)


def test_grant_entry_for_another_hash_or_actor_does_not_count(world):
    a = world.approve()
    rows = world.audit_store._rows[world.tenant.id]
    for i, e in enumerate(rows):
        if e.action == "approval.granted":
            rows[i] = e.model_copy(update={"payload": {**e.payload, "content_hash": "f" * 64}})
    blocked(publish(world, a.id), BlockReason.NO_GRANT_RECORD)


def test_an_operator_role_cannot_approve_on_behalf_of_a_seller(world):
    world.users[world.user_id] = world.users[world.user_id].model_copy(update={"role": UserRole.SELLER})
    with pytest.raises(ApprovalRefused):
        world.approve()


# ---------------------------------------------------------------- 2. a stale approval on revert

def test_revert_after_an_edit_does_not_revive_the_old_approval(world):
    a = world.approve()
    original = world.variants[vid0(world)].rendered_text
    world.edit_after_grant(vid0(world), rendered_text="Edited text")
    world.edit_variant(vid0(world), rendered_text=original)  # back to the approved bytes
    d = publish(world, a.id)
    blocked(d, BlockReason.APPROVAL_VOIDED)
    assert world.bundle.state is BundleState.VOIDED


def test_a_new_grant_voids_the_earlier_one_and_root_hash_must_match(world):
    first = world.approve()
    original = world.variants[vid0(world)].rendered_text
    world.edit_after_grant(vid0(world), rendered_text="Version B")
    world.back_to_review()
    second = world.approve()
    assert world.approvals[first.id].voided_at is not None
    world.edit_variant(vid0(world), rendered_text=original)  # raw revert to A's bytes
    world.approvals[first.id] = world.approvals[first.id].model_copy(update={"voided_at": None})  # forged un-void
    d = publish(world, first.id)
    blocked(d, BlockReason.APPROVAL_STALE)
    assert publish(world, second.id).allowed is False  # B's approval no longer matches A's bytes


def test_new_grant_change_lists_the_voided_approval(world):
    first = world.approve()
    world.edit_after_grant(vid0(world), rendered_text="Version B")
    world.back_to_review()
    world.approvals[first.id] = world.approvals[first.id].model_copy(update={"voided_at": None})
    change = world.approve(return_change=True)
    assert [a.id for a in change.voided_approvals] == [first.id]
    assert [e.action for e in change.audit_entries] == ["approval.voided", "approval.granted"]


# ---------------------------------------------------------------- 3. the kill switch fails closed

def test_system_pause_is_read_from_the_repository(world):
    a = world.approve()
    world.paused_everywhere = True
    blocked(publish(world, a.id), BlockReason.SYSTEM_PAUSED)


def test_system_pause_read_failure_blocks(world):
    a = world.approve()
    world.fail_on = "system_paused"
    blocked(publish(world, a.id), BlockReason.REPO_ERROR)


def test_check_publish_has_no_pause_override(world):
    a = world.approve()
    with pytest.raises(TypeError):
        publish(world, a.id, system_paused=False)


# ---------------------------------------------------------------- 4. the decision carries the payload

def test_decision_carries_the_full_frozen_payload(world):
    a = world.approve()
    d = publish(world, a.id)
    p = d.payload
    assert isinstance(p, PublishPayload) and d.allowed
    assert p.text == "Fino a 120 km con una carica. https://thallo.test/r/abc"
    assert [m.position for m in p.media] == [0, 1] and p.media_sha256 == d.media_sha256
    assert all(m.alt_text == "A red bike" for m in p.media)
    assert p.links == (("https://thallo.test/r/abc", "https://demo-bikes.example/offer?utm=ig"),)
    assert p.variant_hash == d.bundle_hash.variants[vid0(world)]
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.text = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------- 5. links

@pytest.mark.parametrize("text,found", [
    ("Visit www.example.com/x today", ["https://www.example.com/x"]),
    ("Visit example.com/x.", ["https://example.com/x"]),
    ("See https://a.test/x and b.example.org", ["https://a.test/x", "https://b.example.org/"]),
    ("Write to info@demo.example please", []),
    ("Version 1.5 and e.g. a.m.", []),
])
def test_host_like_tokens_count_as_links(text, found):
    assert find_links(text) == found


def test_unresolved_scheme_less_link_blocks(world):
    world.edit_variant(vid0(world), rendered_text="Fino a 120 km. Vai su demo-bikes.example/offerta")
    with pytest.raises(ApprovalRefused):
        world.approve()  # the reviewer cannot see where it goes
    world.links["https://demo-bikes.example/offerta"] = "https://demo-bikes.example/offerta"
    a = world.approve()
    del world.links["https://demo-bikes.example/offerta"]
    d = publish(world, a.id)
    blocked(d, BlockReason.UNRESOLVED_LINK)


def test_tracked_links_of_the_variant_are_bound(world):
    a = world.approve()
    tl = world.tracked[vid0(world)][0]
    world.tracked[vid0(world)][0] = tl.model_copy(update={"destination": "https://evil.example/"})
    blocked(publish(world, a.id), BlockReason.CONTENT_CHANGED)


def test_approval_and_publish_resolve_through_the_same_repository(world):
    content = world.content()
    shown = bundle_hash(content, lambda u: world.resolve_link(world.tenant.id, u)).root
    change = approve_bundle_from_repo(world, tenant_id=world.tenant.id, bundle_id=world.bundle.id,
                                      actor=world.human(), verifier=world.verifier(), shown_hash=shown,
                                      method=ApprovalMethod.PER_BUNDLE, seconds_on_screen=5.0, clock=world.clock,
                                      audit=world.audit, signer=world.signer)
    world.apply(change)
    assert publish(world, change.new_approval.id).allowed


def test_bundle_hash_requires_a_resolver(world):
    with pytest.raises(TypeError):
        bundle_hash(world.content())  # type: ignore[call-arg]


# ---------------------------------------------------------------- 6. audit and row change commit together

def test_grant_returns_the_rows_and_audit_entries_without_writing_them(world):
    content = world.content()
    shown = bundle_hash(content, world.links.get).root
    change = approve_bundle_from_repo(world, tenant_id=world.tenant.id, bundle_id=world.bundle.id,
                                      actor=world.human(), verifier=world.verifier(), shown_hash=shown,
                                      method=ApprovalMethod.PER_BUNDLE, seconds_on_screen=5.0, clock=world.clock,
                                      audit=world.audit, signer=world.signer)
    assert world.audit_store.entries(world.tenant.id) == ()  # nothing written by the gate itself
    assert change.bundle.state is BundleState.APPROVED and change.bundle.root_hash == change.new_approval.content_hash
    assert all(v.state is VariantState.APPROVED for v in change.variants)
    assert [e.action for e in change.audit_entries] == ["approval.granted"]
    assert change.audit_entries[0].subject_id == change.new_approval.id


def test_void_returns_the_rows_and_audit_entries_without_writing_them(world):
    a = world.approve()
    n = len(world.audit_store.entries(world.tenant.id))
    change = void_bundle_approvals(world.bundle, tuple(world.variants.values()), tuple(world.approvals.values()),
                                   reason="text edited", actor=world.human(), clock=world.clock, audit=world.audit,
                                   signer=world.signer)
    assert len(world.audit_store.entries(world.tenant.id)) == n
    assert world.approvals[a.id].voided_at is None  # still live until the caller commits
    assert change.voided_approvals[0].voided_at == world.clock.now()
    assert change.bundle.state is BundleState.VOIDED
    assert all(v.state is VariantState.CHECKING for v in change.variants)
    assert [e.action for e in change.audit_entries] == ["approval.voided"]


def test_void_reason_mentioning_a_session_is_refused(world):
    world.approve()
    with pytest.raises((ApprovalRefused, AuditError)):
        void_bundle_approvals(world.bundle, tuple(world.variants.values()), tuple(world.approvals.values()),
                              reason="session token pasted here", actor=world.human(), clock=world.clock,
                              audit=world.audit, signer=world.signer)


# ---------------------------------------------------------------- 7. verified claims need a verifier

@pytest.mark.parametrize("field", ["verified_by", "verified_at"])
def test_claim_marked_verified_without_a_verifier_blocks(world, field):
    a = world.approve()
    world.edit_claim(next(iter(world.claims)), **{field: None})
    blocked(publish(world, a.id), BlockReason.CLAIM_NOT_VERIFIED)


@pytest.mark.parametrize("field", ["verified_by", "verified_at"])
def test_claim_marked_verified_without_a_verifier_cannot_be_approved(world, field):
    world.edit_claim(next(iter(world.claims)), **{field: None})
    with pytest.raises(ApprovalRefused):
        world.approve()


# ---------------------------------------------------------------- 8. consent

def test_consent_id_is_in_the_hash(world):
    before = bundle_hash(world.content(), world.links.get).root
    mid = next(iter(world.media))
    world.media[mid] = world.media[mid].model_copy(update={"consent_id": world.add_consent()})
    assert bundle_hash(world.content(), world.links.get).root != before


def test_withdrawn_consent_blocks(world):
    mid = next(iter(world.media))
    cid = world.add_consent()
    world.media[mid] = world.media[mid].model_copy(update={"consent_id": cid})
    a = world.approve()
    assert publish(world, a.id).allowed
    world.consents[cid] = world.consents[cid].model_copy(update={"withdrawn_at": world.clock.now()})
    blocked(publish(world, a.id), BlockReason.CONSENT_WITHDRAWN)


def test_missing_consent_blocks(world):
    mid = next(iter(world.media))
    cid = world.add_consent()
    world.media[mid] = world.media[mid].model_copy(update={"consent_id": cid})
    a = world.approve()
    del world.consents[cid]
    blocked(publish(world, a.id), BlockReason.CONSENT_MISSING)


def test_withdrawn_consent_cannot_be_approved(world):
    mid = next(iter(world.media))
    cid = world.add_consent()
    world.consents[cid] = world.consents[cid].model_copy(update={"withdrawn_at": world.clock.now()})
    world.media[mid] = world.media[mid].model_copy(update={"consent_id": cid})
    with pytest.raises(ApprovalRefused):
        world.approve()


# ---------------------------------------------------------------- 9. never raise

@pytest.mark.parametrize("method", ["get_tenant", "get_bundle", "get_variants", "get_variant_media",
                                    "get_variant_claims", "get_tracked_links", "get_consent", "get_approval",
                                    "get_user", "get_audit_entries", "resolve_link", "get_audit_chain",
                                    "audit_anchor", "non_link_allowlist"])
def test_any_repository_failure_blocks_instead_of_raising(world, method):
    mid = next(iter(world.media))
    world.media[mid] = world.media[mid].model_copy(update={"consent_id": world.add_consent()})
    a = world.approve()
    world.fail_on = method
    d = publish(world, a.id)
    assert not d.allowed
    assert set(d.reasons) & {BlockReason.REPO_ERROR, BlockReason.UNHASHABLE, BlockReason.MEDIA_UNREADABLE}


def test_hashing_failure_blocks_instead_of_raising(world):
    a = world.approve()
    world.links["https://thallo.test/r/abc"] = 12  # a broken resolver result
    d = publish(world, a.id)
    assert not d.allowed and BlockReason.UNHASHABLE in d.reasons
