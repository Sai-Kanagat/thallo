"""Canaries, cost breakers and the weekly hand audit, on memory and Postgres."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from thallo.contracts.actors import HumanOnlyViolation
from thallo.contracts.enums import BreakerScope, Capability
from thallo.contracts.ids import new_id
from thallo.contracts.models import CostEntry, Tenant, Variant
from thallo.contracts.states import VariantState
from thallo.gate.decision import BlockReason, check_publish
from thallo.trust import breakers as br
from thallo.trust import canary, hand_audit
from thallo.trust import service as svc
from thallo.trust.ladder import L1, L2, L3
from thallo.trust.records import AuditVerdict, CanaryFlaw, CanaryState, ItemOutcome

from .conftest import AGENT, VERIFIER, at_level, human

T = "offer_post"
GLOBAL = br.GlobalCaps(day_eur=Decimal("50"))


def off() -> bool:
    return False


def mem_reader(world):  # noqa: ANN001, ANN201
    if world.kind == "memory":
        return br.MemorySpend(lambda: list(world.svc.backend.tables.rows.get("cost_ledger", {}).values()))
    return None


def auth(world, tx, est, run="run-1", provider="claude", **kw):  # noqa: ANN001, ANN003, ANN201
    kw.setdefault("global_caps", GLOBAL)
    kw.setdefault("kill_switch", off)
    return br.authorize(tx, world.clock, run_ref=run, provider=provider, estimate_eur=Decimal(est),
                        reader=mem_reader(world), **kw)


def spend(world, tx, amount, run="run-1", provider="claude"):  # noqa: ANN001, ANN201
    return br.record_cost(tx, world.clock, capability=Capability.TEXT, provider=provider,
                          cost_eur=Decimal(amount), run_ref=run)


# ---------------------------------------------------------------- canaries

def test_canary_refused_below_l2_and_blocked_by_the_gate(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        with pytest.raises(canary.CanaryError, match="L2"):
            canary.plant(tx, clock, source_variant_id=a.variant_id, bundle_id=a.bundle_id,
                         flaw=CanaryFlaw.SWAPPED_CLAIM)
        at_level(tx, clock, T, L2)
        v, c = canary.plant(tx, clock, source_variant_id=a.variant_id, bundle_id=a.bundle_id,
                            flaw=CanaryFlaw.SWAPPED_CLAIM)
        src = tx.get(Variant, a.variant_id)
        assert v.is_canary and v.canary_flaw == "swapped_claim" and v.rendered_text != src.rendered_text
        assert "120" in src.rendered_text and "367" in v.rendered_text
        d = check_publish(tx.gate, tenant_id=a.tenant.id, bundle_id=a.bundle_id, variant_id=v.id,
                          approval_id=None, clock=clock, signer=world.svc.signer)
        assert not d.allowed and d.blocks[0].reason is BlockReason.CANARY


def test_missed_canary_drops_and_tells_the_reviewer(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L3)
        v, _ = canary.plant(tx, clock, source_variant_id=a.variant_id, bundle_id=a.bundle_id,
                            flaw=CanaryFlaw.BANNED_PHRASE)
        r = canary.resolve(tx, clock, variant_id=v.id, approved=True, reviewer=human(a))
        assert not r.caught and "planted canary" in r.notice
        assert svc.state(tx, clock, T)[0].level is L2
        assert tx.get(Variant, v.id).state is VariantState.REJECTED
        with pytest.raises(canary.CanaryError):
            canary.resolve(tx, clock, variant_id=v.id, approved=False, reviewer=human(a))
        with pytest.raises(svc.TrustError, match="canary"):
            svc.record_item(tx, clock, task_type=T, subject_kind="variant", subject_id=v.id,
                            outcome=ItemOutcome.CLEAN)


def test_caught_canary_counts_toward_the_catch_rate_not_the_items(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        at_level(tx, clock, T, L2)
        v, _ = canary.plant(tx, clock, source_variant_id=a.variant_id, bundle_id=a.bundle_id,
                            flaw=CanaryFlaw.OFF_BRAND_IMAGE)
        r = canary.resolve(tx, clock, variant_id=v.id, approved=False, reviewer=human(a))
        assert r.caught and r.canary.state is CanaryState.CAUGHT and r.notice is None
        ev = svc.evidence(tx, clock, T)
        assert (ev.canaries_shown, ev.canaries_caught, ev.items) == (1, 1, 0)
        assert canary.due(tx, clock, T) == canary.due(tx, clock, T)  # deterministic


# ---------------------------------------------------------------- breakers

def test_run_and_day_caps_trip_and_stay_tripped(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        assert auth(world, tx, "0.60").allowed
        spend(world, tx, "0.60")
        r = auth(world, tx, "0.50", task_type=T)
        assert not r.allowed and r.trip.subject == "tenant" and r.trip.scope == "run"
        assert not auth(world, tx, "0.01").allowed  # this run stays stopped
        for i in range(5):
            spend(world, tx, "0.90", run=f"r{i}")
        r = auth(world, tx, "0.60", run="new", task_type=T)
        assert not r.allowed and r.trip.scope == "day" and r.paused_task_type  # second trip this week
        assert svc.state(tx, clock, T)[0].paused
        assert not auth(world, tx, "0.01", run="other").allowed
        clock.advance(days=1)
        assert auth(world, tx, "0.10", run="tomorrow").allowed


def test_provider_and_global_caps(world):
    a, b, clock = world.a, world.b, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        br.set_cap(tx, clock, scope=BreakerScope.DAY, provider="gemini", cap_eur=Decimal("0.20"), actor=AGENT)
        assert not auth(world, tx, "0.30", provider="gemini").allowed
        assert auth(world, tx, "0.30", provider="claude").allowed
        spend(world, tx, "0.90", run="g1")
    caps = br.GlobalCaps(day_eur=Decimal("1.00"))
    with world.svc.backend.tenant(b.tenant.id) as tx:  # another tenant's spend counts globally
        r = auth(world, tx, "0.20", run="b1", global_caps=caps)
        assert not r.allowed and r.trip.subject == "global"


def test_fail_closed(world):
    a = world.a
    with world.svc.backend.tenant(a.tenant.id) as tx:
        assert not auth(world, tx, "0.01", kill_switch=None).allowed
        assert not auth(world, tx, "0.01", global_caps=None).allowed
        assert not auth(world, tx, "0.01", kill_switch=lambda: True).allowed

        def boom() -> bool:
            raise RuntimeError("down")
        assert not auth(world, tx, "0.01", kill_switch=boom).allowed
        assert not auth(world, tx, "NaN").allowed
        assert auth(world, tx, "0.01").allowed
    if world.kind == "memory":  # the app role cannot pause a tenant; the owner does
        with world.svc.backend.tenant(a.tenant.id) as tx:
            tx.update(Tenant, a.tenant.id, paused=True)
            assert not auth(world, tx, "0.01").allowed


def test_costs_are_never_negative(world):
    a = world.a
    with world.svc.backend.tenant(a.tenant.id) as tx:
        for bad in ("-1", "NaN", "Infinity"):
            with pytest.raises(svc.TrustError):
                spend(world, tx, bad)
    if world.kind == "postgres":
        import psycopg

        with pytest.raises(psycopg.errors.CheckViolation), world.svc.backend.tenant(a.tenant.id) as tx:
            clock = world.clock
            tx.insert(CostEntry(id=new_id(clock), created_at=clock.now(), tenant_id=a.tenant.id,
                                capability=Capability.TEXT, provider="x", cost_eur=Decimal("-5"), run_ref="r",
                                occurred_at=clock.now()))


def test_raising_a_cap_or_resetting_needs_a_person(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        br.set_cap(tx, clock, scope=BreakerScope.RUN, cap_eur=Decimal("0.50"), actor=AGENT)  # lowering: fine
        with pytest.raises(svc.TrustError):
            br.set_cap(tx, clock, scope=BreakerScope.RUN, cap_eur=Decimal("2"), actor=AGENT)
        with pytest.raises(HumanOnlyViolation):
            br.set_cap(tx, clock, scope=BreakerScope.RUN, cap_eur=Decimal("2"), actor=AGENT, verifier=VERIFIER)
        br.set_cap(tx, clock, scope=BreakerScope.RUN, cap_eur=Decimal("2"), actor=human(a), verifier=VERIFIER)
        spend(world, tx, "1.90")
        trip = auth(world, tx, "0.20").trip
        with pytest.raises(HumanOnlyViolation):
            br.reset_trip(tx, clock, trip.id, actor=AGENT, verifier=VERIFIER)
        assert br.reset_trip(tx, clock, trip.id, actor=human(a), verifier=VERIFIER).reset_by == a.founder.id


# ---------------------------------------------------------------- hand audit

def test_weekly_hand_audit_samples_and_wrong_is_escaped(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        tx.update(Variant, a.variant_id, state=VariantState.APPROVED)
        at_level(tx, clock, T, L3)
        week = hand_audit.monday(tx.get(Variant, a.variant_id).created_at.date())
        s = hand_audit.summary(tx, clock, week)
        assert len(s.sentences) == 1 and "short of 20" in s.note
        assert hand_audit.ensure_week(tx, clock, week) == list(s.sentences)  # stable
        with pytest.raises(HumanOnlyViolation):
            hand_audit.mark(tx, clock, s.sentences[0].id, AuditVerdict.WRONG, actor=AGENT, verifier=VERIFIER)
        row, result = hand_audit.mark(tx, clock, s.sentences[0].id, AuditVerdict.WRONG, actor=human(a),
                                      verifier=VERIFIER)
        assert row.verdict is AuditVerdict.WRONG and result.incident is not None
        assert svc.state(tx, clock, T)[0].level is L1
        s = hand_audit.summary(tx, clock, week)
        assert s.checked == 1 and s.wrong == 1 and s.bound_pct == "100.0"
        assert hand_audit.summary(tx, clock, week + timedelta(days=7)).sentences == ()
