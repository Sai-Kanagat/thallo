"""The evergreen bench fills a missed slot through PLAN_SLOT, and the pre-flight three days ahead
names what would block, on memory and Postgres."""
# ruff: noqa: F811
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.integration.conftest import pg_urls  # noqa: F401
from tests.integration.test_wave1_flow import _id, backend, shop, signer  # noqa: F401
from thallo.contracts.enums import Channel, FunnelStage
from thallo.contracts.models import EventRecord, PlanSlot, Variant
from thallo.contracts.states import PlanSlotState, VariantState
from thallo.gate import check_publish
from thallo.publish import Status, bench_variant, fill_from_bench, mark_missed, preflight, slot_published

from .harness import build_rig


@pytest.fixture
def rig(backend, shop, signer):
    return build_rig(backend, shop, signer)


def add_slot(rig, *, starts_in: timedelta, state=PlanSlotState.OPEN, variant_id=None, channel=Channel.INSTAGRAM):
    now = rig.backend.clock.now()
    with rig.backend.session(rig.t) as s:
        return s.insert(PlanSlot(id=_id(rig.backend), created_at=now, tenant_id=rig.t, week_start=now.date(),
                                 channel=channel, stage=FunnelStage.CONSIDERATION, starts_at=now + starts_in,
                                 state=state, variant_id=variant_id))


def get(rig, model, row_id):
    with rig.backend.session(rig.t) as s:
        return s.get(model, row_id)


def test_a_missed_slot_is_filled_from_the_bench_and_published_once(rig):
    with rig.sessions(rig.t) as ps:
        assert bench_variant(ps.store, rig.shop.variant.id)
    assert get(rig, Variant, rig.shop.variant.id).state is VariantState.BENCHED
    slot = add_slot(rig, starts_in=timedelta(hours=1))
    rig.backend.clock.advance(hours=2)
    with rig.sessions(rig.t) as ps:
        assert [s.id for s in mark_missed(ps.store, rig.backend.clock.now())] == [slot.id]
    with rig.sessions(rig.t) as ps:
        benched = fill_from_bench(ps.store, slot.id)
    assert benched is not None and benched.variant_id == rig.shop.variant.id
    assert get(rig, PlanSlot, slot.id).state is PlanSlotState.BENCHED
    out = rig.publish(variant_id=benched.variant_id, bundle_id=benched.bundle_id)
    assert out.status is Status.SENT and len(rig.poster.calls) == 1
    with rig.sessions(rig.t) as ps:
        assert slot_published(ps.store, slot.id)
        assert fill_from_bench(ps.store, slot.id) is None  # a slot is filled once
    assert get(rig, PlanSlot, slot.id).state is PlanSlotState.PUBLISHED
    with rig.backend.session(rig.t) as s:
        names = [e.name for e in s.list(EventRecord)]
    assert names.count("slot.missed") == 1 and names.count("slot.benched") == 1


def test_the_bench_only_offers_its_channel_and_never_reuses_a_piece(rig):
    with rig.sessions(rig.t) as ps:
        bench_variant(ps.store, rig.shop.variant.id)
    other = add_slot(rig, starts_in=timedelta(hours=-2), channel=Channel.FACEBOOK)
    first = add_slot(rig, starts_in=timedelta(hours=-2))
    second = add_slot(rig, starts_in=timedelta(hours=-2))
    with rig.sessions(rig.t) as ps:
        mark_missed(ps.store, rig.backend.clock.now())
        assert fill_from_bench(ps.store, other.id) is None
        assert fill_from_bench(ps.store, first.id) is not None
        assert fill_from_bench(ps.store, second.id) is None
    assert get(rig, PlanSlot, second.id).state is PlanSlotState.MISSED


def test_a_filled_slot_that_went_out_is_published_not_missed(rig):
    slot = add_slot(rig, starts_in=timedelta(minutes=1), state=PlanSlotState.FILLED, variant_id=rig.shop.variant.id)
    rig.publish()
    rig.backend.clock.advance(hours=1)
    with rig.sessions(rig.t) as ps:
        assert mark_missed(ps.store, rig.backend.clock.now()) == []
    assert get(rig, PlanSlot, slot.id).state is PlanSlotState.PUBLISHED


def run_preflight(rig, slots, account=True, expires=None):
    with rig.sessions(rig.t) as ps:
        return preflight(
            slots, now=rig.backend.clock.now(),
            decide=lambda sl: check_publish(ps.gate, tenant_id=rig.t, bundle_id=rig.shop.bundle.id,
                                            variant_id=sl.variant_id, approval_id=rig.shop.approval.id,
                                            clock=rig.backend.clock, signer=rig.signer),
            account=lambda sl: rig.account if account and sl.channel is rig.account.channel else None,
            approval_expires=lambda sl: expires or rig.shop.approval.expires_at)


def test_preflight_three_days_ahead_passes_a_ready_slot_and_sends_nothing(rig):
    ready = add_slot(rig, starts_in=timedelta(days=2), state=PlanSlotState.FILLED, variant_id=rig.shop.variant.id)
    far = add_slot(rig, starts_in=timedelta(days=5), state=PlanSlotState.FILLED, variant_id=rig.shop.variant.id)
    (check,) = run_preflight(rig, [ready, far])
    assert check.slot_id == ready.id and check.ok, check.problems
    assert rig.poster.calls == []


def test_preflight_names_what_would_block(rig):
    late = add_slot(rig, starts_in=timedelta(days=3), state=PlanSlotState.FILLED, variant_id=rig.shop.variant.id)
    (check,) = run_preflight(rig, [late], account=False, expires=late.starts_at - timedelta(hours=1))
    joined = " ".join(check.problems)
    assert not check.ok and "no channel account" in joined and "expires before the slot" in joined
