"""Plausible Analytics, Stats API v2 (plausible.io/docs/stats-api, read 19 Nov 2026; fixtures are
hand-written from that page, not captured from a live server).

    POST https://plausible.io/api/v2/query     header `Authorization: Bearer <key>`
    body  {site_id, metrics: [...], date_range: [start, end], dimensions: ["time:day"]}
    reply {results: [{dimensions: ["YYYY-MM-DD"], metrics: [n, ...]}], meta, query}

The hosted origin is a constant. Settings hold only the site id (a domain name, validated).
Self-hosted Plausible is not wired: it would need an operator-set origin, as Postiz has.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.analytics.http_base import (
    DEFAULT_TIMEOUT_S,
    AnalyticsHttpProvider,
    PullFailed,
    RawCall,
    Transport,
    finish,
    number,
    raw_ref,
)

NAME = "plausible"
ORIGIN = ("https", "plausible.io", 443)
METRICS = {"visitors": "visitors", "visits": "visits", "pageviews": "pageviews", "bounce_rate": "bounce_rate",
           "visit_duration": "visit_duration", "events": "events"}


class PlausibleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    site_id: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,62}\.)+[a-z]{2,63}$")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, le=120)


class Plausible(AnalyticsHttpProvider):
    name = NAME
    origins = (ORIGIN,)
    settings_model = PlausibleSettings
    metrics_known = tuple(METRICS)
    licence_note = "Plausible Stats API, the tenant's own site data"

    def _pull(self, req: c.AnalyticsRequest) -> c.AnalyticsResult:
        asked = [m for m in req.metrics if m in METRICS]
        if not asked:
            return finish(req, [], {"skipped": "no metric this source knows"})
        body = {"site_id": self.settings.site_id, "metrics": [METRICS[m] for m in asked],
                "date_range": [req.start.isoformat(), req.end.isoformat()], "dimensions": ["time:day"]}
        reply = self._json(RawCall("POST", "https://plausible.io/api/v2/query", json.dumps(body).encode(),
                                   "application/json", self._header_auth("authorization", "Bearer "),
                                   self._timeout()), "the Plausible query")
        raw_ref(reply)  # kept even when the shape check below fails
        results = reply.get("results") if isinstance(reply, dict) else None
        if not isinstance(results, list):
            raise PullFailed("the Plausible reply has an unexpected shape")
        rows: list[dict[str, Any]] = []
        for r in results:
            if not isinstance(r, dict):
                continue
            dims, vals = r.get("dimensions"), r.get("metrics")
            if not (isinstance(dims, list) and dims and isinstance(dims[0], str) and isinstance(vals, list)):
                continue
            for metric, v in zip(asked, vals, strict=False):
                n = number(v)
                if n is not None:
                    rows.append({"date": dims[0][:10], "metric": metric, "value": n, "untrusted": {}})
        return finish(req, rows, reply)


def create(ctx: ProviderContext, transport: Transport | None = None) -> Plausible:
    return Plausible(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.ANALYTICS, NAME, ConnectionMode.HOSTED, create, settings_schema=PlausibleSettings)
