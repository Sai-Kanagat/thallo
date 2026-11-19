"""Run one analytics pull and record it: the raw reply in `signal_pull`, each day's value in
`signal` (kind 'metric'), and every metric that did not come back in `missing`.

The guard against an API that changes or a metric that vanishes is `check_rows`, run on every
provider's output whatever the provider says: a row must be a dict with a date inside the window,
one of the metrics asked for, a finite number and an `untrusted` dict. Anything else is dropped
and counted. A metric with no surviving row is missing: missing is never stored as zero.

A failed pull (no credential, a refused address, an HTTP error, a changed shape) is still a
`signal_pull` row, status 'failed', every metric missing, with a short safe detail. Nothing here
calls a model. Source text (`untrusted`) is stored as data only and never becomes a key.

`tx` is a store `TenantRepo` (one tenant transaction; row-level security applies).
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from thallo.connections.errors import ConnectionsError
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Channel, ConnectionMode
from thallo.contracts.models import Signal
from thallo.signals import context as staging

KIND = "metric"
WEB_SOURCES = {"ga4", "search_console", "plausible", "own_logs"}
NIGHTLY_DAYS = 4  # Search Console data lags two to three days; reruns overlap and the newest value wins


@dataclass(frozen=True)
class PullRecord:
    id: str
    provider: str
    source: str
    status: str
    rows: tuple[dict[str, Any], ...]
    missing: tuple[str, ...]
    dropped: int
    detail: str | None


def check_rows(rows: Sequence[Any], req: c.AnalyticsRequest) -> tuple[list[dict[str, Any]], int]:
    good: list[dict[str, Any]] = []
    dropped = 0
    for r in rows:
        ok = isinstance(r, dict) and r.get("metric") in req.metrics and isinstance(r.get("date"), str)
        if ok:
            try:
                d = date.fromisoformat(r["date"])
            except ValueError:
                ok = False
            else:
                v = r.get("value")
                ok = (req.start <= d <= req.end and isinstance(v, (int, float)) and not isinstance(v, bool)
                      and math.isfinite(float(v)) and isinstance(r.get("untrusted", {}), dict))
        if ok:
            good.append({"date": r["date"], "metric": r["metric"], "value": float(r["value"]),
                         "untrusted": dict(r.get("untrusted", {}))})
        else:
            dropped += 1
    return good, dropped


def _serve(provider: Any, request: c.AnalyticsRequest) -> tuple[c.RunResult, str, ConnectionMode]:
    """Run through a `ConnectionRegistry` (its chain decides who serves) or one provider."""
    if isinstance(provider, ConnectionRegistry):
        served = provider.run(request.tenant_id, request)
        return served.result, served.provider, served.mode
    res = provider.run(request)
    info = provider.describe()
    return res, info.name, info.mode


def pull(tx: Any, provider: Any, request: c.AnalyticsRequest, *,
         uploads: Mapping[str, bytes] | None = None, touches: staging.TouchReader | None = None) -> PullRecord:
    """Run `request` through `provider` (a provider, or a `ConnectionRegistry`) inside a staged
    block, then write what came back. The provider name and mode recorded are the ones that
    actually served, so a fall-through to manual is recorded as manual."""
    if request.tenant_id != tx.tenant_id:
        raise ValueError("the request is for another tenant")
    now = tx.now()
    pull_id = tx.new_id()
    detail: str | None = None
    rows: list[dict[str, Any]] = []
    dropped = 0
    raw: Any = None
    status = "failed"
    provider_name = "unknown"
    missing: tuple[str, ...] = request.metrics
    with staging.staged(tx.tenant_id, uploads=uploads, touches=touches) as s:
        try:
            res, provider_name, mode = _serve(provider, request)
        except psycopg.Error:
            raise
        except ConnectionsError as exc:
            detail = str(exc)[:500]  # our own messages: a status class or a reason, never a body
        except Exception as exc:  # untrusted input broke a parser: keep the type, never the text
            detail = f"the pull failed ({type(exc).__name__})"
        else:
            out = res.output
            if not isinstance(out, c.AnalyticsResult):
                detail = "the provider did not return analytics rows"
            else:
                rows, dropped = check_rows(out.rows, request)
                seen = {r["metric"] for r in rows}
                missing = tuple(m for m in request.metrics if m not in seen)
                if out.raw_ref and out.raw_ref.startswith("sha256:"):
                    raw = s.raw.get(out.raw_ref.removeprefix("sha256:"))
                if mode is ConnectionMode.MANUAL and not rows:
                    status = "manual"
                else:
                    status = "ok" if not missing else "partial"
                if dropped:
                    detail = f"{dropped} row(s) did not match the expected shape and were dropped"
        if raw is None and s.raw:
            raw = list(s.raw.values())[-1]  # a failed pull still keeps what the source said
    raw_sha = None
    if raw is not None:
        raw_sha = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
                                            default=str).encode()).hexdigest()
    tx.execute(
        "INSERT INTO signal_pull (id, created_at, tenant_id, provider, source, window_start, window_end, metrics,"
        " missing, status, detail, row_count, raw, raw_sha256, pulled_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (pull_id, now, tx.tenant_id, provider_name, request.source, request.start, request.end,
         Jsonb(list(request.metrics)), Jsonb(list(missing)), status, detail, len(rows),
         Jsonb(raw) if raw is not None else None, raw_sha, now))
    channel = Channel.WEBSITE if request.source in WEB_SOURCES else None
    for r in rows:
        day = date.fromisoformat(r["date"])
        tx.insert(Signal(id=tx.new_id(), created_at=now, tenant_id=tx.tenant_id, kind=KIND, channel=channel,
                         provider=provider_name,
                         payload={"pull_id": pull_id, "source": request.source, "metric": r["metric"],
                                  "date": r["date"], "value": r["value"], "untrusted": r["untrusted"]},
                         observed_at=now.replace(year=day.year, month=day.month, day=day.day, hour=0, minute=0,
                                                 second=0, microsecond=0)))
    return PullRecord(pull_id, provider_name, request.source, status, tuple(rows), missing, dropped, detail)


def latest(tx: Any, source: str, start: date, end: date) -> dict[str, Any] | None:
    """The latest pull for exactly this source and window: what readers use, so a rerun never
    double counts. Returns {id, status, missing, values: {(date, metric): value}} or None."""
    found = tx.fetch_all(
        "SELECT id::text, status, missing FROM signal_pull WHERE source = %s AND window_start = %s"
        " AND window_end = %s ORDER BY pulled_at DESC, created_at DESC", (source, start, end))
    if not found:
        return None
    pid, status, missing = found[0]
    vals = tx.fetch_all("SELECT payload FROM signal WHERE kind = %s AND payload->>'pull_id' = %s", (KIND, pid))
    return {"id": pid, "status": status, "missing": list(missing),
            "values": {(p["date"], p["metric"]): p["value"] for (p,) in vals}}


def values(tx: Any, source: str, start: date, end: date) -> dict[tuple[str, str], float]:
    """The newest value per (date, metric) for a source, whatever window the pull covered. A
    failed pull writes no values, so it never hides an earlier good one; a day or metric with no
    value in any pull is absent here, which readers show as missing."""
    rows = tx.fetch_all(
        "SELECT DISTINCT ON (payload->>'date', payload->>'metric') payload->>'date', payload->>'metric',"
        " (payload->>'value')::float8 FROM signal WHERE kind = %s AND payload->>'source' = %s"
        " AND (payload->>'date')::date BETWEEN %s AND %s"
        " ORDER BY payload->>'date', payload->>'metric', created_at DESC, id DESC", (KIND, source, start, end))
    return {(d, m): v for d, m, v in rows}


def nightly_window(today: date) -> tuple[date, date]:
    """The last NIGHTLY_DAYS days up to yesterday."""
    return today - timedelta(days=NIGHTLY_DAYS), today - timedelta(days=1)


def run_nightly(tx: Any, provider: Any, jobs: Sequence[tuple[str, tuple[str, ...]]], *,
                touches: staging.TouchReader | None = None) -> list[PullRecord]:
    """One pull per (source, metrics) through `provider` (normally the tenant's
    `ConnectionRegistry`) for the nightly window by the tx clock. A failing source is recorded
    and the rest still run. No scheduler is wired here."""
    start, end = nightly_window(tx.now().date())
    out = []
    for source, metrics in jobs:
        req = c.AnalyticsRequest(tenant_id=tx.tenant_id, source=source, metrics=metrics, start=start, end=end,
                                 idempotency_key=f"signals:{source}:{start.isoformat()}")
        out.append(pull(tx, provider, req, touches=touches))
    return out
