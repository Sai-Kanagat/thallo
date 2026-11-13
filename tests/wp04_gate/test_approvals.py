"""Approvals: human-only, bound to the hash shown, expiring, audited."""
from __future__ import annotations

from datetime import timedelta

import pytest

from cortex_operator.contracts.actors import AgentActor, HumanActor, HumanOnlyViolation, SystemActor
from cortex_operator.contracts.enums import ActorKind, ApprovalMethod, ApprovalSubject
from cortex_operator.contracts.states import BundleState, ClaimState
from cortex_operator.gate import (
    DEFAULT_APPROVAL_TTL,
    MAX_APPROVAL_TTL,
    ApprovalRefused,
    approve_bundle,
    bundle_hash,
    is_expired,
    void_bundle_approvals,
)
from tests.wp04_gate.conftest import make_world


def call(world, **over):
    content = over.pop("content", None) or world.content()
    kw = dict(actor=world.human(), verifier=world.verifier(), shown_hash=bundle_hash(content, world.links.get).root,
              method=ApprovalMethod.PER_BUNDLE, seconds_on_screen=37.5, clock=world.clock, audit=world.audit,
              resolver=world.links.get, approver=world.users[world.user_id], signer=world.signer)
    kw.update(over)
    return approve_bundle(content, **kw)


def test_grant_is_bound_to_the_root_and_expires(world):
    g = call(world)
    a = g.new_approval
    assert a.content_hash == g.bundle_hash.root and a.subject_kind is ApprovalSubject.BUNDLE
    assert a.subject_id == world.bundle.id and a.tenant_id == world.tenant.id and a.approver_id == world.user_id
    assert a.method is ApprovalMethod.PER_BUNDLE and a.seconds_on_screen == 37.5
    assert a.expires_at == a.approved_at + DEFAULT_APPROVAL_TTL and a.canary is False
    assert not is_expired(a, world.clock.now())
    world.clock.advance(DEFAULT_APPROVAL_TTL)
    assert is_expired(a, world.clock.now())
    assert set(g.bundle_hash.variants) == set(world.variants)


@pytest.mark.parametrize("actor", [SystemActor(id="worker"), AgentActor(id="wp-07", model_id="m")])
def test_machines_cannot_approve(world, actor):
    with pytest.raises(HumanOnlyViolation):
        call(world, actor=actor)


def test_a_dead_session_cannot_approve(world):
    with pytest.raises(HumanOnlyViolation):
        call(world, actor=HumanActor(id=world.user_id, tenant_id=world.tenant.id, session_id="stolen"))


def test_a_person_from_another_tenant_cannot_approve(world):
    other = make_world()
    with pytest.raises(HumanOnlyViolation):
        call(world, actor=other.human(), verifier=other.verifier())


def test_refused_if_content_changed_after_it_was_shown(world):
    shown = bundle_hash(world.content(), world.links.get).root
    world.edit_variant(next(iter(world.variants)), rendered_text="Something else")
    with pytest.raises(ApprovalRefused):
        call(world, shown_hash=shown)
    assert world.audit_store.entries(world.tenant.id) == ()


def test_refused_unless_in_review(world):
    world.bundle = world.bundle.model_copy(update={"state": BundleState.OPEN})
    with pytest.raises(ApprovalRefused):
        call(world)


@pytest.mark.parametrize("update", [
    {"state": ClaimState.CANDIDATE},
    {"evidence_sha256": None},
    {"expires_at": None, "state": ClaimState.EXPIRED},
])
def test_refused_with_an_unproven_claim(world, update):
    world.edit_claim(next(iter(world.claims)), **update)
    with pytest.raises(ApprovalRefused):
        call(world)


def test_refused_with_an_expired_claim(world):
    world.edit_claim(next(iter(world.claims)), expires_at=world.clock.now())
    with pytest.raises(ApprovalRefused):
        call(world)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1), MAX_APPROVAL_TTL + timedelta(seconds=1)])
def test_lifetime_is_bounded(world, ttl):
    with pytest.raises(ApprovalRefused):
        call(world, ttl=ttl)


@pytest.mark.parametrize("secs", [-1, float("nan"), float("inf")])
def test_seconds_on_screen_must_be_sane(world, secs):
    with pytest.raises(ApprovalRefused):
        call(world, seconds_on_screen=secs)


def test_approver_must_be_a_user_id(world):
    person = HumanActor(id="sai", tenant_id=world.tenant.id, session_id=world.session)
    with pytest.raises(ApprovalRefused):
        call(world, actor=person)


def test_canary_bundle_can_be_approved_and_is_marked(world):
    world.edit_variant(next(iter(world.variants)), is_canary=True)
    assert call(world).new_approval.canary is True


def test_grant_and_void_are_audited_without_a_session(world):
    g = call(world)
    world.apply(g)
    a = g.new_approval
    change = void_bundle_approvals(world.bundle, tuple(world.variants.values()), (a,), reason="claim C-0012 changed",
                                   actor=world.human(), clock=world.clock, audit=world.audit, signer=world.signer)
    world.apply(change)
    voided = world.approvals[a.id]
    assert voided.voided_at == world.clock.now() and voided.void_reason == "claim C-0012 changed"
    again = void_bundle_approvals(world.bundle, tuple(world.variants.values()), (voided,), reason="again",
                                  actor=world.human(), clock=world.clock, audit=world.audit, signer=world.signer)
    assert again.voided_approvals == () and again.audit_entries == ()  # already void: nothing to do
    entries = world.audit_store.entries(world.tenant.id)
    assert [e.action for e in entries] == ["approval.granted", "approval.voided"]
    assert entries[0].actor_kind is ActorKind.HUMAN and entries[0].actor_id == world.user_id
    assert entries[0].payload["content_hash"] == a.content_hash
    assert world.session not in repr([e.model_dump() for e in entries])
    assert world.audit.verify(world.tenant.id).ok


def test_void_needs_a_reason(world):
    a = call(world).new_approval
    with pytest.raises(ApprovalRefused):
        void_bundle_approvals(world.bundle, (), (a,), reason="  ", actor=world.human(), clock=world.clock,
                              audit=world.audit, signer=world.signer)
