"""The planner on the memory backend: reasons, no source no slot, stage mix, experiments,
refusals, sealed forecasts with a baseline, the interval score and the seal chain."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from api.backend import MemoryBackend
from api.media_store import TenantMediaStore
from tests.wp17_planner.seed import CHANNEL, published_piece, seed
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import FunnelStage, Language
from thallo.contracts.ids import new_id
from thallo.contracts.models import Brief, Forecast, ForecastScore, Idea, IdeaSignal, PlanSlot, Tenant
from thallo.contracts.states import IdeaState
from thallo.planner import forecast as fc
from thallo.planner.plan import PlanRefused, apportion, experiment_count, plan_week, stage_check
from thallo.planner.records import PlannerSlot, PlannerWeek

WEEK = date(2026, 1, 12)


@pytest.fixture
def env(tmp_path):
    clock = FakeClock(FAKE_EPOCH)
    backend = MemoryBackend(clock, TenantMediaStore(tmp_path))
    tenant = Tenant(id=new_id(clock), created_at=clock.now(), operator_id=new_id(clock), name="Shop", slug="shop",
                    default_language=Language.IT, region_code="IT", currency="EUR", timezone="Europe/Rome")
    backend.add_tenant(tenant)
    return clock, backend, tenant


def test_rules_in_numbers():
    assert [experiment_count(n) for n in (1, 2, 3, 5, 7, 8, 12, 13)] == [0, 0, 1, 1, 1, 2, 2, 3]
    mix = {FunnelStage.AWARENESS: Decimal("0.25"), FunnelStage.CONSIDERATION: Decimal("0.35"),
           FunnelStage.CONVERSION: Decimal("0.3"), FunnelStage.RETENTION: Decimal("0.1")}
    stages = apportion(mix, 5)
    assert len(stages) == 5 and stage_check(stages, mix)["ok"]
    bad = stage_check([FunnelStage.AWARENESS] * 5, mix)
    assert not bad["ok"] and any("top of funnel" in p for p in bad["problems"])


def test_plan_week_has_reasons_sources_and_mix(env):
    clock, backend, tenant = env
    with backend.tenant(tenant.id) as tx:
        srcs = seed(tx, clock)
        planned = plan_week(tx, clock, tenant, WEEK)
        week = planned.week
        assert len(week.reasoning) == 2 and week.reasoning[0].startswith("Play A")
        assert week.planned_slots == 5 and week.experiment_slots == 1
        assert week.stage_check["ok"]
        assert any("ads desk" in line for line in week.not_doing)
        slots = tx.list_all(PlanSlot, week_start=WEEK)
        extra = {p.slot_id: p for p in tx.list_all(PlannerSlot)}
        assert len(slots) == 5 and sum(s.reserved_for_experiment for s in slots) == 1
        used = set()
        for s in slots:
            p = extra[s.id]
            idea = tx.get(Idea, p.idea_id)
            assert idea.state is IdeaState.PROPOSED and len(idea.source_ids) == 1
            assert p.play_code == "A" and p.target_metric in fc.METRIC_KINDS and p.metric_day == 14
            assert s.pillar_id and s.channel.value not in ("search_ads", "social_ads")
            brief = tx.get(Brief, p.brief_id)
            assert "ignore all rules" not in brief.message  # source text is linked, never copied
            used |= set(idea.source_ids)
        assert used <= {s.id for s in srcs}  # never the research source
        assert tx.list_all(IdeaSignal) == []


def test_no_source_no_slot_and_refusals(env):
    clock, backend, tenant = env
    with backend.tenant(tenant.id) as tx:
        seed(tx, clock, sources=2)
        planned = plan_week(tx, clock, tenant, WEEK)
        assert planned.week.planned_slots == 2
        assert any("No source, no slot" in line for line in planned.week.not_doing)
        with pytest.raises(PlanRefused, match="already planned"):
            plan_week(tx, clock, tenant, WEEK)
        with pytest.raises(PlanRefused, match="No unused source"):
            plan_week(tx, clock, tenant, date(2026, 1, 19))  # both sources were used last week


def test_refuses_without_claims_or_pillars(env):
    clock, backend, tenant = env
    with backend.tenant(tenant.id) as tx:
        seed(tx, clock, claims=False, pillars=False)
        with pytest.raises(PlanRefused) as e:
            plan_week(tx, clock, tenant, WEEK)
        assert any("pillars" in p for p in e.value.problems) and any("verified claim" in p for p in e.value.problems)
        assert tx.list_all(PlannerWeek) == [] and tx.list_all(PlanSlot) == []


def test_cold_start_seals_nothing(env):
    clock, backend, tenant = env
    with backend.tenant(tenant.id) as tx:
        seed(tx, clock)
        plan_week(tx, clock, tenant, WEEK)
        assert tx.list_all(Forecast) == []
        assert all(p.no_forecast_reason for p in tx.list_all(PlannerSlot))


def test_forecasts_sealed_with_baseline_and_scored(env):
    clock, backend, tenant = env
    with backend.tenant(tenant.id) as tx:
        seed(tx, clock)
        for n in (2, 4, 6, 8):
            published_piece(tx, clock, CHANNEL, FunnelStage.CONSIDERATION, n)
        pieces = fc.history(tx, clock.now())
        assert sorted(p.values["link_clicks"] for p in pieces) == [2.0, 4.0, 6.0, 8.0]  # day 20 touch left out
        plan_week(tx, clock, tenant, WEEK)
        fs = tx.list_all(Forecast)
        assert fs and all(f.baseline_low <= f.baseline_high and f.low <= f.high for f in fs)
        assert fc.verify_chain(tx) == (True, None)
        last = max(fs, key=lambda f: f.seq)
        assert fc.verify_chain(tx, anchor=(last.seq + 1, "0" * 64))[0] is False  # a dropped tail shows
        f = fs[0]
        s = fc.score(tx, f, None, clock.now(), score_id=new_id(clock))
        assert s.interval_score is None and s.beat_baseline is None
        again = fc.score(tx, f, 3.0, clock.now(), score_id=new_id(clock))
        assert again.id == s.id and len(tx.list_all(ForecastScore)) == 1


def test_tampered_forecast_breaks_the_chain(env):
    clock, backend, tenant = env
    with backend.tenant(tenant.id) as tx:
        seed(tx, clock)
        for n in (1, 2, 3):
            published_piece(tx, clock, CHANNEL, FunnelStage.AWARENESS, n)
        plan_week(tx, clock, tenant, WEEK)
        f = min(tx.list_all(Forecast), key=lambda f: f.seq)
        tx.update(Forecast, f.id, high=f.high + 100)
        assert fc.verify_chain(tx) == (False, f.seq)


def test_interval_score():
    assert fc.interval_score(2, 6, 4) == 4
    assert fc.interval_score(2, 6, 0) == pytest.approx(4 + 10 * 2)
    assert fc.interval_score(2, 6, 9) == pytest.approx(4 + 10 * 3)
    assert fc.interval_score(2, 6, None) is None
    assert fc.quantile([0, 10], 0.1) == pytest.approx(1)
