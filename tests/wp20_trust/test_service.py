"""Autonomy state per tenant and task type, on memory and Postgres."""
from __future__ import annotations

import pytest

from thallo.contracts.actors import HumanActor, HumanOnlyViolation
from thallo.contracts.enums import IncidentSeverity
from thallo.contracts.events import EventName
from thallo.contracts.models import AutonomyState, EventRecord, Incident
from thallo.trust import service as svc
from thallo.trust.ladder import L0, L1, L2, L3, L4
from thallo.trust.records import ItemOutcome as O
from thallo.trust.records import TrustTask

from .conftest import AGENT, VERIFIER, at_level, human, items

T = "offer_post"


def level(tx, clock, task_type=T):  # noqa: ANN001, ANN201
    return svc.state(tx, clock, task_type)[0].level


def test_new_task_type_starts_in_shadow_and_climbs_only_by_a_person(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        assert level(tx, clock) is L0
        items(tx, clock, T, [O.CLEAN] * 20)
        assert svc.state(tx, clock, T)[1].calibration_done
        with pytest.raises(svc.Refused, match="Voice rules"):
            svc.raise_level(tx, clock, T, actor=human(a), verifier=VERIFIER)
        svc.sign_voice_rules(tx, clock, T, actor=human(a), verifier=VERIFIER)
        with pytest.raises(HumanOnlyViolation):
            svc.raise_level(tx, clock, T, actor=AGENT, verifier=VERIFIER)
        stale = HumanActor(id=a.founder.id, tenant_id=a.tenant.id, session_id="not-live")
        with pytest.raises(HumanOnlyViolation):
            svc.raise_level(tx, clock, T, actor=stale, verifier=VERIFIER)
        ch = svc.raise_level(tx, clock, T, actor=human(a), verifier=VERIFIER)
        assert (ch.from_level, ch.to_level) == (L0, L1)
        # the window restarts at the new level: no double raise on the same evidence
        with pytest.raises(svc.Refused, match="0 of 50"):
            svc.raise_level(tx, clock, T, actor=human(a), verifier=VERIFIER)
        events = [e for e in tx.list_all(EventRecord) if e.name == EventName.AUTONOMY_CHANGED.value]
        assert events and events[-1].actor_id == a.founder.id
        assert tx.audit.verify(a.tenant.id).ok


def test_l1_to_l2_on_the_stated_bound(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L1)
        # two defects, spread out so the last-20 rule never fires, plus a typo that does not count
        outcomes = [O.CLEAN] * 50
        outcomes[5] = O.DEFECT
        outcomes[30] = O.DEFECT
        outcomes[40] = O.TYPO
        items(tx, clock, T, outcomes)
        assert level(tx, clock) is L1
        ch = svc.raise_level(tx, clock, T, actor=human(a), verifier=VERIFIER)
        assert ch.to_level is L2 and "12.1%" in ch.reason


def test_hard_rule_hit_goes_to_l1_with_an_incident_same_moment(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L3)
        (r,) = items(tx, clock, T, [O.HARD_RULE])
        assert level(tx, clock) is L1
        assert r.incident is not None and r.incident.severity is IncidentSeverity.HARD_RULE
        assert [c.to_level for c in r.changes] == [L1]


def test_escaped_defect_found_later_goes_to_l1(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L4)
        items(tx, clock, T, [O.CLEAN], prefix="x")
        r = svc.record_item(tx, clock, task_type=T, subject_kind="bundle", subject_id="x-0", outcome=O.ESCAPED)
        assert level(tx, clock) is L1 and r.incident.severity is IncidentSeverity.HIGH
        with pytest.raises(svc.TrustError, match="already recorded"):
            svc.record_item(tx, clock, task_type=T, subject_kind="bundle", subject_id="x-0", outcome=O.CLEAN)


def test_two_defects_in_last_twenty_drop_one(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L3)
        results = items(tx, clock, T, [O.DEFECT] + [O.CLEAN] * 18 + [O.DEFECT])
        assert level(tx, clock) is L2
        assert results[-1].changes[0].reason.startswith("2 defects")
        # the window restarted: the next defect alone does not drop again
        items(tx, clock, T, [O.DEFECT], prefix="y")
        assert level(tx, clock) is L2


def test_defects_far_apart_do_not_drop(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L3)
        items(tx, clock, T, [O.DEFECT] + [O.CLEAN] * 19 + [O.DEFECT])
        assert level(tx, clock) is L3


def test_version_change_drops_one_and_must_be_reearned(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L2)
        assert svc.note_version(tx, clock, T, "sonnet-5.5/p1") == ()  # the baseline
        (ch,) = svc.note_version(tx, clock, T, "sonnet-5.5/p2")
        assert ch.to_level is L1
        items(tx, clock, T, [O.CLEAN] * 50)
        assert svc.state(tx, clock, T)[1].clean_since_version == 50
        svc.raise_level(tx, clock, T, actor=human(a), verifier=VERIFIER)
        assert level(tx, clock) is L2


def test_version_reearn_blocks_raise_until_30_clean(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L1)
        svc.note_version(tx, clock, T, "v1")
        items(tx, clock, T, [O.CLEAN] * 50)
        svc.note_version(tx, clock, T, "v2")  # L1 to L0
        assert level(tx, clock) is L0
        meta = svc.state(tx, clock, T)[1]
        tx.update(TrustTask, meta.id, voice_rules_signed_by=a.founder.id)
        items(tx, clock, T, [O.CLEAN] * 20, prefix="z")
        with pytest.raises(svc.Refused, match="20 of 30 clean"):
            svc.raise_level(tx, clock, T, actor=human(a), verifier=VERIFIER)
        items(tx, clock, T, [O.CLEAN] * 10, prefix="w")
        assert svc.raise_level(tx, clock, T, actor=human(a), verifier=VERIFIER).to_level is L1


def test_region_policy_and_any_incident_drop(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, "carousel", L4)
        at_level(tx, clock, T, L3)
        svc.region_policy_changed(tx, clock, ["carousel"], "IT-2026-11")
        assert level(tx, clock, "carousel") is L1
        assert level(tx, clock) is L3
        inc, (ch,) = svc.open_incident(tx, clock, severity=IncidentSeverity.LOW, title="late post", task_type=T)
        assert ch.to_level is L2 and tx.get(Incident, inc.id) is not None


def test_state_is_per_tenant(world):
    clock = world.clock
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        at_level(tx, clock, T, L3)
    with world.svc.backend.tenant(world.b.tenant.id) as tx:
        assert level(tx, clock) is L0
        assert len(tx.list_all(AutonomyState)) == 1
