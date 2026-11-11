"""Clock, ids, actors, enums and state machines."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

import pytest

from cortex_operator.contracts import Status
from cortex_operator.contracts.actors import AgentActor, HumanActor, HumanOnlyViolation, SystemActor, require_human
from cortex_operator.contracts.clock import FAKE_EPOCH, Clock, FakeClock, SystemClock
from cortex_operator.contracts.enums import Capability, HumanOnlyAction
from cortex_operator.contracts.ids import is_id, new_id
from cortex_operator.contracts.states import LEGACY_STATUS, MACHINES, VARIANT, IllegalTransition, VariantState


def test_fake_clock_moves_forward_only():
    clk = FakeClock(FAKE_EPOCH)
    assert isinstance(clk, Clock) and isinstance(SystemClock(), Clock)
    assert clk.advance(days=1) == FAKE_EPOCH + timedelta(days=1)
    with pytest.raises(ValueError):
        clk.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        clk.set(FAKE_EPOCH)


def test_fake_clock_refuses_naive_time():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))  # noqa: DTZ001


def test_ids_are_uuid7_sortable_and_deterministic_on_request():
    clk = FakeClock(FAKE_EPOCH)
    a = new_id(clk, bytes(10))
    assert is_id(a) and a == new_id(clk, bytes(10))
    clk.advance(milliseconds=5)
    assert new_id(clk) > a
    assert is_id(new_id(clk)) and not is_id("C-0001")


class FakeVerifier:
    def __init__(self, live: set[str]) -> None:
        self.live = live

    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id in self.live


@pytest.mark.parametrize("actor", [SystemActor(id="worker"), AgentActor(id="wp-07", model_id="m")])
@pytest.mark.parametrize("action", list(HumanOnlyAction))
def test_human_only_actions_refuse_machines(actor, action):
    with pytest.raises(HumanOnlyViolation):
        require_human(actor, action, verifier=FakeVerifier({"s1"}), tenant_id="t1")


def test_human_only_actions_accept_a_verified_person():
    sai = HumanActor(id="u1", tenant_id="t1", session_id="s1")
    assert require_human(sai, HumanOnlyAction.APPROVE, verifier=FakeVerifier({"s1"}), tenant_id="t1") is sai


def test_a_self_made_human_is_refused():
    fake = HumanActor(id="agent-pretending", tenant_id="t1", session_id="made-up")
    with pytest.raises(HumanOnlyViolation):
        require_human(fake, HumanOnlyAction.SIGN_SPEND, verifier=FakeVerifier({"s1"}), tenant_id="t1")


def test_a_human_from_another_tenant_is_refused():
    other = HumanActor(id="u2", tenant_id="t2", session_id="s1")
    with pytest.raises(HumanOnlyViolation):
        require_human(other, HumanOnlyAction.APPROVE, verifier=FakeVerifier({"s1"}), tenant_id="t1")


def test_human_actor_needs_a_session():
    with pytest.raises(ValueError):
        HumanActor(id="u1", tenant_id="t1", session_id="")


def test_sixteen_capabilities():
    assert len(Capability) == 16


@pytest.mark.parametrize("name", sorted(MACHINES))
def test_every_state_is_reachable_and_terminals_exist(name):
    m = MACHINES[name]
    seen, todo = {m.initial}, deque([m.initial])
    while todo:
        for nxt in m.transitions[todo.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == set(m.states), f"unreachable in {name}: {set(m.states) - seen}"
    assert m.terminal(), f"{name} has no terminal state"


def test_illegal_transition_raises():
    with pytest.raises(IllegalTransition):
        VARIANT.check(VariantState.DRAFTING, VariantState.PUBLISHED)
    with pytest.raises(IllegalTransition):
        VARIANT.check(VariantState.REVIEW, VariantState.SCHEDULED)  # never schedule without approval
    assert VARIANT.check(VariantState.REVIEW, VariantState.APPROVED) is VariantState.APPROVED


def test_publishing_is_only_reachable_through_approval():
    into_scheduled = {s for s, ts in VARIANT.transitions.items() if VariantState.SCHEDULED in ts}
    assert into_scheduled == {VariantState.APPROVED, VariantState.BENCHED, VariantState.FAILED}
    into_benched = {s for s, ts in VARIANT.transitions.items() if VariantState.BENCHED in ts}
    assert into_benched == {VariantState.APPROVED}
    into_approved = {s for s, ts in VARIANT.transitions.items() if VariantState.APPROVED in ts}
    assert into_approved == {VariantState.REVIEW}


def test_reapproval_always_reruns_checks():
    into_review = {s for s, ts in VARIANT.transitions.items() if VariantState.REVIEW in ts}
    assert into_review == {VariantState.CHECKING, VariantState.ON_HOLD}
    for s in (VariantState.APPROVED, VariantState.BENCHED, VariantState.SCHEDULED, VariantState.FAILED):
        assert VariantState.REVIEW not in VARIANT.transitions[s]


def test_legacy_status_fully_mapped():
    assert set(LEGACY_STATUS) == set(Status)
