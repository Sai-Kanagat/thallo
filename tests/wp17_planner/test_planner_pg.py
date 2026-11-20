"""The planner on Postgres: a week planned through the app role, forecasts append-only, the
chain head kept by the database where the app role cannot write it."""
from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest

from tests.wp01_store.conftest import URL, make_tenant
from tests.wp17_planner.seed import seed
from thallo.contracts.ids import new_id
from thallo.contracts.models import Forecast, PlanSlot
from thallo.planner import forecast as fc
from thallo.planner.plan import plan_week
from thallo.planner.records import ForecastHead, PlannerSlot, PlannerWeek

pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")


def _prop():
    return fc.Proposal("link_clicks", 1.0, 5.0, 0.0, 6.0, "test")


def test_plan_and_forecast_chain_on_postgres(database):
    tenant = make_tenant(database, "Planner")
    clock = database.clock
    with database.tenant(tenant.id) as tx:
        seed(tx, clock)
        planned = plan_week(tx, clock, tenant, date(2026, 1, 12))
        assert len(tx.list_all(PlanSlot)) == 5 and len(tx.list_all(PlannerSlot)) == 5
        assert tx.list_all(PlannerWeek)[0].reasoning == planned.week.reasoning
        briefs = [p.brief_id for p in tx.list_all(PlannerSlot)]
        for b in briefs[:2]:
            clock.advance(seconds=1)
            fc.seal(tx, forecast_id=new_id(clock), brief_id=b, proposal=_prop(), now=clock.now())
        head = tx.list_all(ForecastHead)
        assert len(head) == 1 and head[0].seq == 2
        assert fc.verify_chain(tx) == (True, None)
        first = min(tx.list_all(Forecast), key=lambda f: f.seq)
    with pytest.raises(psycopg.Error), database.tenant(tenant.id) as tx:
        tx.update(Forecast, first.id, high=99.0)
    with pytest.raises(psycopg.Error), database.tenant(tenant.id) as tx:
        tx.execute("DELETE FROM forecast WHERE id = %s", (first.id,))
    with pytest.raises(psycopg.Error), database.tenant(tenant.id) as tx:
        tx.execute("UPDATE forecast_head SET seq = 1")
    with pytest.raises(psycopg.Error), database.tenant(tenant.id) as tx:  # must extend the head
        bad = first.model_copy(update={"id": new_id(clock), "seq": 9, "brief_id": briefs[2]})
        tx.insert(bad)
    with database.tenant(tenant.id) as tx:
        assert fc.verify_chain(tx) == (True, None)
        assert fc.verify_chain(tx, anchor=(3, "0" * 64)) == (False, 3)


def test_mirror_runs_are_append_only(database):
    from thallo.planner.records import PlannerMirror
    tenant = make_tenant(database, "Mirror")
    clock = database.clock
    with database.tenant(tenant.id) as tx:
        row = tx.insert(PlannerMirror(id=new_id(clock), created_at=clock.now(), tenant_id=tenant.id,
                                      week_start=date(2026, 1, 12), provider="manual", mode="manual",
                                      status="ok", mirrored=0, mirrored_at=clock.now() + timedelta(0)))
    with pytest.raises(psycopg.Error), database.tenant(tenant.id) as tx:
        tx.update(PlannerMirror, row.id, mirrored=3)
