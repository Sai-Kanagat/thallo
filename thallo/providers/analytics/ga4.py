"""Google Analytics 4, Data API v1beta runReport (developers.google.com/analytics/devguides/
reporting/data/v1/rest/v1beta/properties/runReport, read 19 Nov 2026; fixtures are hand-written
from that page, not captured from a live server).

    POST https://analyticsdata.googleapis.com/v1beta/properties/{property}:runReport
    body  {dateRanges: [{startDate, endDate}], dimensions: [{name: "date"}], metrics: [{name}],
           keepEmptyRows: false}
    reply {metricHeaders: [{name}], rows: [{dimensionValues: [{value: "YYYYMMDD"}],
           metricValues: [{value: "12"}]}], rowCount}

Values are matched to metrics through `metricHeaders` (reverse-mapped through METRICS), not by
position alone, so a reordered or renamed metric is missing rather than misfiled. A day GA4 does
not return is absent, never zero. Auth: google_base.py.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.analytics.google_base import TOKEN_ORIGIN, GoogleProvider
from thallo.providers.analytics.http_base import (
    DEFAULT_TIMEOUT_S,
    PullFailed,
    RawCall,
    Transport,
    finish,
    number,
    raw_ref,
)

NAME = "ga4"
API_ORIGIN = ("https", "analyticsdata.googleapis.com", 443)
METRICS = {"sessions": "sessions", "users": "totalUsers", "new_users": "newUsers", "pageviews": "screenPageViews",
           "engaged_sessions": "engagedSessions", "conversions": "keyEvents"}
_BACK = {v: k for k, v in METRICS.items()}


class Ga4Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    property_id: str = Field(pattern=r"^[0-9]{1,20}$")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, le=120)


class Ga4(GoogleProvider):
    name = NAME
    origins = (TOKEN_ORIGIN, API_ORIGIN)
    settings_model = Ga4Settings
    metrics_known = tuple(METRICS)
    licence_note = "Google Analytics Data API, the tenant's own property"

    def _pull(self, req: c.AnalyticsRequest) -> c.AnalyticsResult:
        asked = [m for m in req.metrics if m in METRICS]
        if not asked:
            return finish(req, [], {"skipped": "no metric this source knows"})
        auth = self._bearer()
        body = {"dateRanges": [{"startDate": req.start.isoformat(), "endDate": req.end.isoformat()}],
                "dimensions": [{"name": "date"}], "metrics": [{"name": METRICS[m]} for m in asked],
                "keepEmptyRows": False}
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{self.settings.property_id}:runReport"
        reply = self._json(RawCall("POST", url, json.dumps(body).encode(), "application/json", auth,
                                   self._timeout()), "the GA4 report")
        raw_ref(reply)  # kept even when the shape check below fails
        if not isinstance(reply, dict) or not isinstance(reply.get("metricHeaders", []), list):
            raise PullFailed("the GA4 reply has an unexpected shape")
        headers = [_BACK.get(h["name"]) if isinstance(h, dict) and isinstance(h.get("name"), str) else None
                   for h in reply.get("metricHeaders", [])]
        rows: list[dict[str, Any]] = []
        found = reply.get("rows") or []
        if not isinstance(found, list):
            raise PullFailed("the GA4 reply has an unexpected shape")
        for r in found:
            if not isinstance(r, dict):
                continue
            dims, vals = r.get("dimensionValues"), r.get("metricValues")
            if not (isinstance(dims, list) and dims and isinstance(dims[0], dict) and isinstance(vals, list)):
                continue
            raw_day = dims[0].get("value")
            if not (isinstance(raw_day, str) and len(raw_day) == 8 and raw_day.isdigit()):
                continue
            day = f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}"
            for metric, v in zip(headers, vals, strict=False):
                n = number(v.get("value")) if isinstance(v, dict) else None
                if metric is not None and n is not None:
                    rows.append({"date": day, "metric": metric, "value": n, "untrusted": {}})
        return finish(req, rows, reply)


def create(ctx: ProviderContext, transport: Transport | None = None) -> Ga4:
    return Ga4(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.ANALYTICS, NAME, ConnectionMode.HOSTED, create, settings_schema=Ga4Settings)
