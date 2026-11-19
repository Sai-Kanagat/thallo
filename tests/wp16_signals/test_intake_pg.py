"""Intake on Postgres: raw pulls kept, missing stored as missing, failed pulls recorded, latest
pull wins, tenant isolation, and the pull log is append-only."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg
import pytest

from tests.wp01_store.conftest import URL, make_tenant
from thallo.connections.errors import ProviderUnavailable
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH
from thallo.contracts.enums import ConnectionMode
from thallo.signals import intake
from thallo.signals.touches import reader

pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")
DAY = date(2026, 1, 1)


class Fake:
    """A provider double returning fixed rows (shape checks run on them like any provider)."""

    def __init__(self, rows, missing=(), raw=None, fail=None):
        self.rows, self.missing, self.raw, self.fail = rows, missing, raw, fail

    def run(self, request):
        if self.fail:
            raise self.fail
        from thallo.providers.analytics.http_base import raw_ref
        out = c.AnalyticsResult(rows=tuple(self.rows), missing=self.missing,
                                raw_ref=raw_ref(self.raw) if self.raw is not None else None)
        return c.RunResult(output=out, cost_eur=Decimal(0),
                           provenance=c.Provenance(made_by="provider", provider="fake", mode=ConnectionMode.HOSTED,
                                                   made_at=FAKE_EPOCH))



def areq(tenant_id, *metrics):
    return c.AnalyticsRequest(tenant_id=tenant_id, source="ga4", metrics=metrics, start=DAY, end=DAY)


@pytest.fixture
def tenant(database):
    return make_tenant(database, "Signals")


def test_pull_keeps_raw_and_stores_missing(database, tenant):
    rows = [{"date": "2026-01-01", "metric": "sessions", "value": 0, "untrusted": {}},
            {"date": "2026-01-01", "metric": "users", "value": float("nan"), "untrusted": {}},
            {"date": "2026-02-09", "metric": "sessions", "value": 3, "untrusted": {}},
            {"date": "2026-01-01", "metric": "injected", "value": 1, "untrusted": {}}]
    with database.tenant(tenant.id) as tx:
        rec = intake.pull(tx, Fake(rows, raw={"rows": ["x"]}), areq(tenant.id, "sessions", "users"),
                          provider_name="ga4")
        assert rec.status == "partial" and rec.missing == ("users",) and rec.dropped == 3
        got = intake.latest(tx, "ga4", DAY, DAY)
        assert got["values"] == {("2026-01-01", "sessions"): 0.0} and got["missing"] == ["users"]
        raw, sha = tx.fetch_all("SELECT raw, raw_sha256 FROM signal_pull WHERE id = %s", (rec.id,))[0]
        assert raw == {"rows": ["x"]} and len(sha) == 64


def test_failed_pull_is_recorded_with_everything_missing(database, tenant):
    with database.tenant(tenant.id) as tx:
        rec = intake.pull(tx, Fake([], fail=ProviderUnavailable("not configured")), areq(tenant.id, "sessions"),
                          provider_name="ga4")
        assert rec.status == "failed" and rec.missing == ("sessions",)
        assert tx.fetch_all("SELECT count(*) FROM signal WHERE payload->>'pull_id' = %s", (rec.id,))[0][0] == 0


def test_latest_pull_wins(database, tenant):
    with database.tenant(tenant.id) as tx:
        intake.pull(tx, Fake([{"date": "2026-01-01", "metric": "sessions", "value": 5, "untrusted": {}}]),
                    areq(tenant.id, "sessions"), provider_name="ga4")
        database.clock.advance(seconds=5)
        intake.pull(tx, Fake([{"date": "2026-01-01", "metric": "sessions", "value": 7, "untrusted": {}}]),
                    areq(tenant.id, "sessions"), provider_name="ga4")
        assert intake.latest(tx, "ga4", DAY, DAY)["values"] == {("2026-01-01", "sessions"): 7.0}


def test_other_tenant_sees_nothing_and_log_is_append_only(database, tenant):
    other = make_tenant(database, "Other")
    with database.tenant(tenant.id) as tx:
        intake.pull(tx, Fake([]), areq(tenant.id, "sessions"), provider_name="ga4")
    with database.tenant(other.id) as tx:
        assert intake.latest(tx, "ga4", DAY, DAY) is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("DELETE FROM signal_pull")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("UPDATE signal_pull SET status = 'ok'")


def test_own_logs_through_intake(database, tenant):
    from thallo.connections.registry import ProviderContext
    from thallo.contracts.enums import Capability
    from thallo.contracts.models import Connection
    from thallo.providers.analytics import own_logs

    with database.tenant(tenant.id) as tx:
        row = Connection(id=tx.new_id(), created_at=tx.now(), tenant_id=tenant.id, capability=Capability.ANALYTICS,
                         provider="own_logs", mode=ConnectionMode.LOCAL)
        prov = own_logs.create(ProviderContext(tenant.id, row, database.clock))
        req = c.AnalyticsRequest(tenant_id=tenant.id, source="own_logs", metrics=("link_clicks",), start=DAY, end=DAY)
        rec = intake.pull(tx, prov, req, provider_name="own_logs", touches=reader(tx))
        assert rec.status == "ok" and rec.rows[0]["value"] == 0.0
