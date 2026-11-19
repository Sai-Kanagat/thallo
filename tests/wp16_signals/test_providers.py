"""Analytics providers: the contract suite on recorded fixtures, missing is never zero, source
text never becomes a metric, and each credential goes only where it belongs. Sockets raise."""
from __future__ import annotations

import json
import os
import socket
from datetime import date
from pathlib import Path

import pytest

from thallo.connections import InMemorySecretStore, KeyRing, ProviderContext, ProviderUnavailable, Vault
from thallo.connections.registry import ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import T, _id
from thallo.providers.analytics import csv_import, ga4, own_logs, plausible, postiz_analytics, search_console
from thallo.providers.analytics.http_base import PullFailed, RawCall, RawReply
from thallo.signals.context import staged

FIX = json.loads((Path(__file__).parent / "fixtures.json").read_text())
GOOGLE_CRED = json.dumps({"client_id": "cid", "client_secret": "fake-client-secret", "refresh_token": "fake-refresh"})
FAKE_KEY = "fake-analytics-key-for-tests-only"
START, END = date(2026, 1, 1), date(2026, 1, 7)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


class Fixtures:
    """Answers by host from the fixture file and records every call."""

    def __init__(self, **by_host: str) -> None:
        self.by_host = by_host
        self.calls: list[RawCall] = []

    def __call__(self, call: RawCall) -> RawReply:
        self.calls.append(call)
        host = call.url.split("/")[2]
        f = FIX[self.by_host[host]]
        return RawReply(f["status"], f["body"])


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock):
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


def ctx(clock, vault, name, settings, secret=FAKE_KEY, mode=ConnectionMode.HOSTED):
    ref = vault.put(T, secret) if secret else None
    row = Connection(id=_id(80), created_at=clock.now(), tenant_id=T, capability=Capability.ANALYTICS,
                     provider=name, mode=mode, settings=settings, secret_ref=ref)
    return ProviderContext(T, row, clock, vault)


def req(*metrics, source="web", refs=()):
    return c.AnalyticsRequest(tenant_id=T, source=source, metrics=metrics, start=START, end=END, refs=refs)


GOOGLE_HOSTS = {"oauth2.googleapis.com": "google_token"}


def make_ga4(clock, vault, report="ga4_report", **kw):
    t = Fixtures(**GOOGLE_HOSTS, **{"analyticsdata.googleapis.com": report})
    return ga4.create(ctx(clock, vault, "ga4", {"property_id": "123456"}, secret=GOOGLE_CRED, **kw), transport=t), t


def make_gsc(clock, vault, **kw):
    t = Fixtures(**GOOGLE_HOSTS, **{"searchconsole.googleapis.com": "gsc_query"})
    s = {"site": "sc-domain:example.com"}
    return search_console.create(ctx(clock, vault, "search_console", s, secret=GOOGLE_CRED, **kw), transport=t), t


def make_plausible(clock, vault, **kw):
    t = Fixtures(**{"plausible.io": "plausible_query"})
    return plausible.create(ctx(clock, vault, "plausible", {"site_id": "example.com"}, **kw), transport=t), t


def make_postiz(clock, vault, fixture="postiz_platform", **kw):
    t = Fixtures(**{"api.postiz.com": fixture})
    return postiz_analytics.create(ctx(clock, vault, "postiz_analytics", {"integration_id": "ig-1"}, **kw),
                                   transport=t), t


# ---------------------------------------------------------------- the contract on fixtures

@pytest.mark.parametrize("make,metrics", [(make_ga4, ("sessions", "users")), (make_gsc, ("clicks", "impressions")),
                                          (make_plausible, ("visitors", "pageviews")),
                                          (make_postiz, ("followers", "impressions"))])
def test_http_providers_pass_the_contract(clock, vault, make, metrics):
    prov, _ = make(clock, vault)
    assert_provider_contract(prov, clock, requests=[req(*metrics)])
    assert not prov.describe().sends_externally
    assert_provider_contract(prov, clock)  # the default sample: metrics it may not know


def test_csv_and_own_logs_pass_the_contract(clock, vault):
    csv_p = csv_import.create(ctx(clock, vault, "csv_import", {}, secret=None, mode=ConnectionMode.LOCAL))
    data = b"date,clicks,impressions\n2026-01-01,3,40\n"
    with staged(T, uploads={"u1": data}):
        assert_provider_contract(csv_p, clock, requests=[req("clicks", "impressions", refs=("u1",))])
    logs = own_logs.create(ctx(clock, vault, "own_logs", {}, secret=None, mode=ConnectionMode.LOCAL))
    with staged(T, touches=lambda s, e: [(date(2026, 1, 2), "link", 5)]):
        assert_provider_contract(logs, clock, requests=[req("link_clicks", "forms")])


def test_all_register_through_discovery():
    names = set(ProviderRegistry().discover().names(Capability.ANALYTICS))
    assert {"csv_import", "own_logs", "ga4", "search_console", "plausible", "postiz_analytics"} <= names


# ---------------------------------------------------------------- missing is missing

def test_ga4_zero_is_zero_and_junk_is_missing(clock, vault):
    prov, _ = make_ga4(clock, vault)
    out = prov.run(req("sessions", "users", "conversions")).output
    vals = {(r["date"], r["metric"]): r["value"] for r in out.rows}
    assert vals[("2026-01-02", "sessions")] == 0.0
    assert ("2026-01-02", "users") not in vals
    assert out.missing == ("conversions",)


def test_ga4_renamed_metric_is_missing_not_misfiled(clock, vault):
    prov, _ = make_ga4(clock, vault, report="ga4_renamed")
    out = prov.run(req("sessions")).output
    assert out.rows == () and out.missing == ("sessions",)


def test_unknown_metric_is_missing_without_a_call(clock, vault):
    prov, t = make_plausible(clock, vault)
    out = prov.run(req("clicks")).output
    assert out.missing == ("clicks",) and t.calls == []


def test_plausible_null_is_missing(clock, vault):
    prov, _ = make_plausible(clock, vault)
    vals = {(r["date"], r["metric"]) for r in prov.run(req("visitors", "pageviews")).output.rows}
    assert ("2026-01-02", "visitors") not in vals and ("2026-01-02", "pageviews") in vals


def test_postiz_labels_never_become_metrics_and_empty_is_missing(clock, vault):
    prov, _ = make_postiz(clock, vault)
    out = prov.run(req("followers", "impressions")).output
    assert {r["metric"] for r in out.rows} == {"followers"}
    assert out.missing == ("impressions",)
    empty, _ = make_postiz(clock, vault, fixture="postiz_empty")
    assert empty.run(req("followers")).output.missing == ("followers",)


def test_csv_blank_dash_and_text_are_missing(clock, vault):
    p = csv_import.create(ctx(clock, vault, "csv_import", {}, secret=None, mode=ConnectionMode.LOCAL))
    data = b"Date,Clicks,Impressions,=cmd()\n2026-01-01,0,\n2026-01-02,-,12\n2026-01-03,lots,5\n2025-12-01,9,9\n"
    with staged(T, uploads={"u": data}):
        out = p.run(req("clicks", "impressions", refs=("u",))).output
    vals = {(r["date"], r["metric"]): r["value"] for r in out.rows}
    assert vals == {("2026-01-01", "clicks"): 0.0, ("2026-01-02", "impressions"): 12.0,
                    ("2026-01-03", "impressions"): 5.0}


def test_csv_without_staging_falls_to_manual(clock, vault):
    p = csv_import.create(ctx(clock, vault, "csv_import", {}, secret=None, mode=ConnectionMode.LOCAL))
    with pytest.raises(ProviderUnavailable):
        p.estimate(req("clicks", refs=("u",)))


# ---------------------------------------------------------------- failures and where secrets go

def test_http_error_is_pull_failed_without_body(clock, vault):
    t = Fixtures(**{"plausible.io": "error_500"})
    prov = plausible.create(ctx(clock, vault, "plausible", {"site_id": "example.com"}), transport=t)
    with pytest.raises(PullFailed) as e:
        prov.run(req("visitors"))
    assert "secret internal" not in str(e.value) and "5xx" in str(e.value)


def test_unconfigured_is_unavailable(clock, vault):
    prov, _ = make_plausible(clock, vault, secret=None)
    with pytest.raises(ProviderUnavailable):
        prov.run(req("visitors"))
    bad = plausible.create(ctx(clock, vault, "plausible", {"site_id": "https://evil.example/x"}))
    assert bad.problem() is not None


def test_google_credential_only_to_token_origin_and_bearer_only_to_api(clock, vault):
    prov, t = make_ga4(clock, vault)
    prov.run(req("sessions"))
    token_call, api_call = t.calls
    assert token_call.url.startswith("https://oauth2.googleapis.com/") and token_call.auth is None
    assert b"fake-client-secret" in token_call.body
    assert api_call.url.startswith("https://analyticsdata.googleapis.com/")
    assert b"fake-client-secret" not in api_call.body and b"fake-refresh" not in api_call.body
    assert api_call.auth.scheme == "Bearer " and api_call.auth.secret.reveal() == "ya29.fake-access-token"


def test_call_to_another_origin_is_refused(clock, vault):
    prov, t = make_plausible(clock, vault)
    from thallo.providers.analytics.http_base import TransportError
    with pytest.raises(TransportError):
        prov._call(RawCall("GET", "https://evil.example/steal"))
    assert t.calls == []


def test_search_console_site_is_validated_and_encoded(clock, vault):
    prov, t = make_gsc(clock, vault)
    prov.run(req("clicks"))
    assert "/sites/sc-domain%3Aexample.com/searchAnalytics/query" in t.calls[1].url
    for bad in ("sc-domain:example.com/../x", "http://example.com/", "https://example.com/path/"):
        assert search_console.create(ctx(clock, vault, "search_console", {"site": bad},
                                         secret=GOOGLE_CRED)).problem() is not None
