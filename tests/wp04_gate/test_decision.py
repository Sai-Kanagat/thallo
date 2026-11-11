"""BUILD_PLAN Verification, gate tests: each case must block, with a reason."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from cortex_operator.contracts.states import BundleState, ClaimState, VariantState
from cortex_operator.gate import BlockReason, SpendCheck, check_publish, decide, void_approval
from tests.wp04_gate.conftest import make_world


def first_variant(world):
    return next(iter(world.variants))


def publish(world, approval_id, variant_id=None, **kw):
    return check_publish(world, tenant_id=world.tenant.id, bundle_id=world.bundle.id,
                         variant_id=variant_id or first_variant(world), approval_id=approval_id,
                         clock=world.clock, **kw)


def assert_blocked(decision, reason: BlockReason):
    assert decision.allowed is False
    assert reason in decision.reasons, decision.blocks
    assert decision.publish_text is None and decision.media_sha256 == ()
    assert all(b.detail for b in decision.blocks)


def test_approved_untouched_bundle_is_allowed(world):
    a = world.approve()
    d = publish(world, a.id)
    assert d.allowed, d.blocks
    assert d.reason == "ok" and d.content_hash == a.content_hash == d.bundle_hash.root
    assert d.publish_text == "Fino a 120 km con una carica. https://thallo.test/r/abc"
    assert len(d.media_sha256) == 2


def test_no_approval_blocks(world):
    world.approve()
    assert_blocked(publish(world, None), BlockReason.NO_APPROVAL)


def test_unknown_approval_id_blocks(world):
    world.approve()
    assert_blocked(publish(world, world.bundle.id), BlockReason.NO_APPROVAL)


def test_changed_text_blocks(world):
    a = world.approve()
    world.edit_variant(first_variant(world), rendered_text="Fino a 150 km con una carica. https://thallo.test/r/abc")
    assert_blocked(publish(world, a.id), BlockReason.CONTENT_CHANGED)


def test_changed_text_in_another_variant_blocks_this_one(world):
    a = world.approve()
    other = list(world.variants)[1]
    world.edit_variant(other, rendered_text="Our new bike is here!")
    assert_blocked(publish(world, a.id), BlockReason.CONTENT_CHANGED)


def test_whitespace_only_change_does_not_block(world):
    a = world.approve()
    vid = first_variant(world)
    world.edit_variant(vid, rendered_text="  Fino a 120 km   con una carica.\t https://thallo.test/r/abc \r\n\r\n")
    assert publish(world, a.id).allowed


def test_changed_image_bytes_at_same_path_blocks(world):
    a = world.approve()
    world.blobs["s3://bucket/hero.png"] = b"\x89PNG a different image"
    d = publish(world, a.id)
    assert_blocked(d, BlockReason.CONTENT_CHANGED)
    assert BlockReason.MEDIA_BYTES_MISMATCH in d.reasons


def test_changed_image_bytes_with_row_hash_rewritten_still_blocks(world):
    """Someone swaps the file and also rewrites media.sha256 to match: the root still moves."""
    import hashlib

    a = world.approve()
    world.blobs["s3://bucket/hero.png"] = b"\x89PNG swapped"
    mid = next(m.id for m in world.media.values() if m.storage_uri == "s3://bucket/hero.png")
    world.media[mid] = world.media[mid].model_copy(update={"sha256": hashlib.sha256(b"\x89PNG swapped").hexdigest()})
    d = publish(world, a.id)
    assert_blocked(d, BlockReason.CONTENT_CHANGED)
    assert BlockReason.MEDIA_BYTES_MISMATCH not in d.reasons


def test_unreadable_media_blocks(world):
    a = world.approve()
    del world.blobs["s3://bucket/detail.png"]
    assert_blocked(publish(world, a.id), BlockReason.MEDIA_UNREADABLE)


def test_changed_claim_evidence_blocks(world):
    a = world.approve()
    cid = next(iter(world.claims))
    world.edit_claim(cid, evidence_sha256="b" * 64)  # state still verified: only the hash catches it
    assert_blocked(publish(world, a.id), BlockReason.CONTENT_CHANGED)


def test_changed_claim_value_blocks(world):
    a = world.approve()
    world.edit_claim(next(iter(world.claims)), value="150")
    assert_blocked(publish(world, a.id), BlockReason.CONTENT_CHANGED)


def test_changed_rule_version_blocks(world):
    a = world.approve()
    world.bundle = world.bundle.model_copy(update={"rule_version": 4})
    assert_blocked(publish(world, a.id), BlockReason.CONTENT_CHANGED)


def test_changed_link_destination_blocks(world):
    a = world.approve()
    world.links["https://thallo.test/r/abc"] = "https://elsewhere.example/"
    assert_blocked(publish(world, a.id), BlockReason.CONTENT_CHANGED)


def test_expired_approval_blocks(world):
    a = world.approve()
    world.clock.set(a.expires_at)
    assert_blocked(publish(world, a.id), BlockReason.APPROVAL_EXPIRED)


def test_voided_approval_blocks(world):
    a = world.approve()
    world.approvals[a.id] = void_approval(a, reason="claim changed", actor=world.human(), clock=world.clock,
                                          audit=world.audit)
    assert_blocked(publish(world, a.id), BlockReason.APPROVAL_VOIDED)


def test_expired_claim_blocks(world):
    a = world.approve()
    cid = next(iter(world.claims))
    world.clock.set(world.claims[cid].expires_at)
    assert_blocked(publish(world, a.id), BlockReason.CLAIM_EXPIRED)


def test_expired_claim_blocks_while_approval_is_still_valid(world):
    cid = next(iter(world.claims))
    world.edit_claim(cid, expires_at=world.clock.now() + timedelta(days=1))
    a = world.approve()
    world.clock.advance(days=2)
    d = publish(world, a.id)
    assert world.clock.now() < a.expires_at
    assert_blocked(d, BlockReason.CLAIM_EXPIRED)
    assert d.reasons == (BlockReason.CLAIM_EXPIRED,)


def test_claim_no_longer_verified_blocks(world):
    a = world.approve()
    world.edit_claim(next(iter(world.claims)), state=ClaimState.VOIDED)
    assert_blocked(publish(world, a.id), BlockReason.CLAIM_NOT_VERIFIED)


@pytest.mark.parametrize("variant_index", [0, 1])
def test_canary_is_always_refused(variant_index):
    """A canary bundle with a valid approval, untouched content and no other problem: refused."""
    world = make_world()
    canary_vid = list(world.variants)[variant_index]
    world.edit_variant(canary_vid, is_canary=True, canary_flaw="claim swapped")
    a = world.approve()
    assert a.canary is True
    for vid in world.variants:  # whichever variant the publisher asks for
        d = publish(world, a.id, variant_id=vid)
        assert_blocked(d, BlockReason.CANARY)
        assert d.reasons[0] is BlockReason.CANARY
        assert d.reasons == (BlockReason.CANARY,)


def test_canary_flag_on_approval_alone_is_refused(world):
    a = world.approve()
    world.approvals[a.id] = a.model_copy(update={"canary": True})
    assert_blocked(publish(world, a.id), BlockReason.CANARY)


def test_canary_refused_even_with_every_other_input_wrong_or_right(world):
    vid = first_variant(world)
    world.edit_variant(vid, is_canary=True)
    a = world.approve()
    content = world.content()
    for approval in (a, None):
        for paused in (True, False):
            tenant = world.tenant.model_copy(update={"paused": paused})
            d = decide(content, approval, tenant=tenant, variant_id=vid, now=world.clock.now())
            assert_blocked(d, BlockReason.CANARY)


def test_spend_over_cap_blocks(world):
    a = world.approve()
    d = publish(world, a.id, spend=SpendCheck(requested_eur=Decimal("30"), spent_eur=Decimal("80"),
                                              cap_eur=Decimal("100")))
    assert_blocked(d, BlockReason.SPEND_OVER_CAP)


def test_spend_without_a_signed_cap_blocks(world):
    a = world.approve()
    assert_blocked(publish(world, a.id, spend=SpendCheck(requested_eur=Decimal("1"))), BlockReason.SPEND_OVER_CAP)


def test_spend_within_cap_is_allowed(world):
    a = world.approve()
    d = publish(world, a.id, spend=SpendCheck(requested_eur=Decimal("20"), spent_eur=Decimal("80"),
                                              cap_eur=Decimal("100")))
    assert d.allowed


@pytest.mark.parametrize("spend", [
    SpendCheck(requested_eur=Decimal("-5"), cap_eur=Decimal("100")),
    SpendCheck(requested_eur=Decimal("NaN"), cap_eur=Decimal("100")),
    SpendCheck(requested_eur=5.0, cap_eur=Decimal("100")),  # type: ignore[arg-type]
])
def test_invalid_spend_blocks(world, spend):
    a = world.approve()
    assert_blocked(publish(world, a.id, spend=spend), BlockReason.SPEND_INVALID)


def test_paused_brand_blocks(world):
    a = world.approve()
    world.tenant = world.tenant.model_copy(update={"paused": True})
    assert_blocked(publish(world, a.id), BlockReason.BRAND_PAUSED)


def test_system_kill_switch_blocks(world):
    a = world.approve()
    assert_blocked(publish(world, a.id, system_paused=True), BlockReason.SYSTEM_PAUSED)


def test_approval_cannot_be_reused_for_a_different_bundle(world):
    """A second bundle with identical content: the first bundle's approval does not cover it."""
    a = world.approve()
    content = world.content()
    clone = content.bundle.model_copy(update={"id": world.tenant.id[:-1] + "0"})  # another valid id
    variants = tuple(type(vc)(variant=vc.variant.model_copy(update={"bundle_id": clone.id}), media=vc.media,
                              claims=vc.claims) for vc in content.variants)
    other = type(content)(bundle=clone, variants=variants)
    d = decide(other, a, tenant=world.tenant, variant_id=variants[0].variant.id, now=world.clock.now())
    assert_blocked(d, BlockReason.APPROVAL_WRONG_SUBJECT)
    assert BlockReason.CONTENT_CHANGED in d.reasons  # the root binds the bundle id as well


def test_approval_cannot_be_reused_for_a_different_tenant(world):
    a = world.approve()
    other = make_world()
    other.approve()
    # Hand tenant B's publisher tenant A's approval id: the repository is tenant-scoped, so it is not found.
    other.approvals[a.id] = a
    d = check_publish(other, tenant_id=other.tenant.id, bundle_id=other.bundle.id,
                      variant_id=next(iter(other.variants)), approval_id=a.id, clock=other.clock)
    assert_blocked(d, BlockReason.NO_APPROVAL)
    # And if a faulty repository did return it, the decision itself refuses it.
    d = decide(other.content(), a, tenant=other.tenant, variant_id=next(iter(other.variants)),
               now=other.clock.now())
    assert_blocked(d, BlockReason.APPROVAL_WRONG_TENANT)
    assert BlockReason.APPROVAL_WRONG_SUBJECT in d.reasons and BlockReason.CONTENT_CHANGED in d.reasons


def test_rows_from_another_tenant_block(world):
    a = world.approve()
    cid = next(iter(world.claims))
    world.edit_claim(cid, tenant_id=make_world().tenant.id)
    assert_blocked(publish(world, a.id), BlockReason.TENANT_MISMATCH)


def test_bundle_not_in_approved_state_blocks(world):
    a = world.approve()
    world.bundle = world.bundle.model_copy(update={"state": BundleState.VOIDED})
    assert_blocked(publish(world, a.id), BlockReason.BUNDLE_NOT_APPROVED)


def test_variant_not_publishable_blocks(world):
    a = world.approve()
    world.edit_variant(first_variant(world), state=VariantState.REJECTED)
    assert_blocked(publish(world, a.id), BlockReason.VARIANT_NOT_PUBLISHABLE)


def test_variant_outside_the_bundle_blocks(world):
    a = world.approve()
    assert_blocked(publish(world, a.id, variant_id=world.bundle.id), BlockReason.VARIANT_NOT_IN_BUNDLE)


def test_missing_bundle_blocks(world):
    a = world.approve()
    d = check_publish(world, tenant_id=world.tenant.id, bundle_id=a.id, variant_id=first_variant(world),
                      approval_id=a.id, clock=world.clock)
    assert_blocked(d, BlockReason.NO_BUNDLE)


def test_unrendered_slot_in_outgoing_text_blocks(world):
    vid = first_variant(world)
    world.edit_variant(vid, rendered_text=None)
    a = world.approve()
    assert_blocked(publish(world, a.id, variant_id=vid), BlockReason.UNRENDERED_SLOT)


def test_slot_without_linked_claim_blocks(world):
    vid = list(world.variants)[1]
    world.edit_variant(vid, text="Now {{C-0099}} more.", rendered_text="Now 3x more.")
    a = world.approve()
    assert_blocked(publish(world, a.id, variant_id=vid), BlockReason.CLAIM_SLOT_UNLINKED)


def test_approval_from_the_future_blocks(world):
    a = world.approve()
    world.approvals[a.id] = a.model_copy(update={"approved_at": world.clock.now() + timedelta(hours=1)})
    assert_blocked(publish(world, a.id), BlockReason.APPROVAL_NOT_YET_VALID)
