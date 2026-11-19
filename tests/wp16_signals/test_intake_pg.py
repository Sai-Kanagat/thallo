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

    def describe(self):
        return c.ProviderInfo(capability=c.Capability.ANALYTICS, name="ga4", mode=ConnectionMode.HOSTED)

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
        rec = intake.pull(tx, Fake(rows, raw={"rows": ["x"]}), areq(tenant.id, "sessions", "users"))
        assert rec.status == "partial" and rec.missing == ("users",) and rec.dropped == 3
        got = intake.latest(tx, "ga4", DAY, DAY)
        assert got["values"] == {("2026-01-01", "sessions"): 0.0} and got["missing"] == ["users"]
        raw, sha = tx.fetch_all("SELECT raw, raw_sha256 FROM signal_pull WHERE id = %s", (rec.id,))[0]
        assert raw == {"rows": ["x"]} and len(sha) == 64


def test_failed_pull_is_recorded_with_everything_missing(database, tenant):
    with database.tenant(tenant.id) as tx:
        rec = intake.pull(tx, Fake([], fail=ProviderUnavailable("not configured")), areq(tenant.id, "sessions"))
        assert rec.status == "failed" and rec.missing == ("sessions",)
        assert tx.fetch_all("SELECT count(*) FROM signal WHERE payload->>'pull_id' = %s", (rec.id,))[0][0] == 0


def test_latest_pull_wins(database, tenant):
    with database.tenant(tenant.id) as tx:
        intake.pull(tx, Fake([{"date": "2026-01-01", "metric": "sessions", "value": 5, "untrusted": {}}]),
                    areq(tenant.id, "sessions"))
        database.clock.advance(seconds=5)
        intake.pull(tx, Fake([{"date": "2026-01-01", "metric": "sessions", "value": 7, "untrusted": {}}]),
                    areq(tenant.id, "sessions"))
        assert intake.latest(tx, "ga4", DAY, DAY)["values"] == {("2026-01-01", "sessions"): 7.0}


def test_other_tenant_sees_nothing_and_log_is_append_only(database, tenant):
    other = make_tenant(database, "Other")
    with database.tenant(tenant.id) as tx:
        intake.pull(tx, Fake([]), areq(tenant.id, "sessions"))
    with database.tenant(other.id) as tx:
        assert intake.latest(tx, "ga4", DAY, DAY) is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("DELETE FROM signal_pull")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("UPDATE signal_pull SET status = 'ok'")


def test_own_logs_counts_real_touches_by_utc_day(database, tenant):
    from datetime import UTC, datetime

    from thallo.connections.registry import ProviderContext
    from thallo.contracts.enums import Capability
    from thallo.contracts.models import Connection
    from thallo.providers.analytics import own_logs

    assert database.clock.now().date() > date(2026, 1, 2)
    with database.tenant(tenant.id) as tx:
        for when, kind in [(datetime(2026, 1, 1, 9, tzinfo=UTC), "form"),
                           (datetime(2026, 1, 1, 23, 30, tzinfo=UTC), "form"),
                           (datetime(2026, 1, 2, 0, 10, tzinfo=UTC), "call")]:
            tx.execute("INSERT INTO touch (id, created_at, tenant_id, kind, occurred_at) VALUES (%s, %s, %s, %s, %s)",
                       (tx.new_id(), tx.now(), tenant.id, kind, when))
        row = Connection(id=tx.new_id(), created_at=tx.now(), tenant_id=tenant.id, capability=Capability.ANALYTICS,
                         provider="own_logs", mode=ConnectionMode.LOCAL)
        prov = own_logs.create(ProviderContext(tenant.id, row, database.clock))
        req = c.AnalyticsRequest(tenant_id=tenant.id, source="own_logs", metrics=("forms", "calls"),
                                 start=date(2026, 1, 1), end=date(2026, 1, 2))
        rec = intake.pull(tx, prov, req, touches=reader(tx))
        vals = {(r["date"], r["metric"]): r["value"] for r in rec.rows}
        assert vals == {("2026-01-01", "forms"): 2.0, ("2026-01-01", "calls"): 0.0,
                        ("2026-01-02", "forms"): 0.0, ("2026-01-02", "calls"): 1.0}


def test_newest_value_wins_across_windows_and_failures_hide_nothing(database, tenant):
    one = {"date": "2026-01-01", "metric": "sessions", "untrusted": {}}
    with database.tenant(tenant.id) as tx:
        week = c.AnalyticsRequest(tenant_id=tenant.id, source="ga4", metrics=("sessions",), start=DAY,
                                  end=date(2026, 1, 7))
        intake.pull(tx, Fake([one | {"value": 5}]), week)
        database.clock.advance(seconds=5)
        intake.pull(tx, Fake([one | {"value": 6}]), areq(tenant.id, "sessions"))
        database.clock.advance(seconds=5)
        intake.pull(tx, Fake([], fail=ProviderUnavailable("down")), areq(tenant.id, "sessions"))
        assert intake.values(tx, "ga4", DAY, date(2026, 1, 7)) == {("2026-01-01", "sessions"): 6.0}


def test_parser_crash_on_untrusted_input_is_a_failed_pull(database, tenant):
    with database.tenant(tenant.id) as tx:
        rec = intake.pull(tx, Fake([], fail=TypeError("attacker text here")), areq(tenant.id, "sessions"))
        assert rec.status == "failed" and rec.detail == "the pull failed (TypeError)"


def test_registry_path_csv_staged_and_manual_when_not(database, tenant):
    import os

    from thallo.connections import ConnectionRegistry, InMemoryConnectionStore, InMemorySecretStore, KeyRing, Vault
    from thallo.connections.registry import ProviderRegistry
    from thallo.contracts.enums import Capability

    clock = database.clock
    reg = ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                             connections=InMemoryConnectionStore(),
                             vault=Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock), environ={})
    reg.add(tenant.id, Capability.ANALYTICS, "csv_import")
    req = c.AnalyticsRequest(tenant_id=tenant.id, source="csv", metrics=("clicks",), start=DAY, end=DAY,
                             refs=("u1",), idempotency_key="csv:u1")
    with database.tenant(tenant.id) as tx:
        rec = intake.pull(tx, reg, req, uploads={"u1": b"date,clicks\n2026-01-01,4\n"})
        assert rec.provider == "csv_import" and rec.status == "ok"
        assert tx.fetch_all("SELECT channel FROM signal WHERE payload->>'pull_id' = %s", (rec.id,)) == [(None,)]
        manual = intake.pull(tx, reg, req.model_copy(update={"idempotency_key": "csv:u2"}))
        assert manual.provider == "manual" and manual.status == "manual" and manual.missing == ("clicks",)
