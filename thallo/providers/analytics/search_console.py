"""Google Search Console, Search Analytics query (developers.google.com/webmaster-tools/v1/
searchanalytics/query, read 19 Nov 2026; fixtures are hand-written from that page, not captured
from a live server).

    POST https://searchconsole.googleapis.com/webmasters/v3/sites/{siteUrl}/searchAnalytics/query
    body  {startDate, endDate, dimensions: ["date"], rowLimit}
    reply {rows: [{keys: ["YYYY-MM-DD"], clicks, impressions, ctr, position}], responseAggregationType}

The site is a Search Console property id, not a host we call: `sc-domain:example.com` or
`https://www.example.com/`, validated by pattern and percent-encoded into the path of the constant
API origin. Only day totals are pulled, so no query text enters Thallo here. Auth: google_base.py.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.analytics.google_base import TOKEN_ORIGIN, GoogleProvider
from thallo.providers.analytics.http_base import DEFAULT_TIMEOUT_S, PullFailed, RawCall, Transport, finish, number

NAME = "search_console"
API_ORIGIN = ("https", "searchconsole.googleapis.com", 443)
METRICS = ("clicks", "impressions", "ctr", "position")
_HOST = r"[a-z0-9]([a-z0-9-]{0,62}\.)+[a-z]{2,63}"


class SearchConsoleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    site: str = Field(pattern=rf"^(sc-domain:{_HOST}|https://{_HOST}/)$")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, le=120)


class SearchConsole(GoogleProvider):
    name = NAME
    origins = (TOKEN_ORIGIN, API_ORIGIN)
    settings_model = SearchConsoleSettings
    metrics_known = METRICS
    licence_note = "Google Search Console API, the tenant's own property"

    def _pull(self, req: c.AnalyticsRequest) -> c.AnalyticsResult:
        if not any(m in METRICS for m in req.metrics):
            return finish(req, [], {"skipped": "no metric this source knows"})
        auth = self._bearer()
        body = {"startDate": req.start.isoformat(), "endDate": req.end.isoformat(), "dimensions": ["date"],
                "rowLimit": 1000}
        site = quote(self.settings.site, safe="")
        url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
        reply = self._json(RawCall("POST", url, json.dumps(body).encode(), "application/json", auth,
                                   self._timeout()), "the Search Console query")
        if not isinstance(reply, dict):
            raise PullFailed("the Search Console reply has an unexpected shape")
        rows: list[dict[str, Any]] = []
        for r in reply.get("rows") or []:
            keys = r.get("keys") if isinstance(r, dict) else None
            if not (isinstance(keys, list) and keys and isinstance(keys[0], str)):
                continue
            for metric in METRICS:
                n = number(r.get(metric))
                if n is not None:
                    rows.append({"date": keys[0][:10], "metric": metric, "value": n, "untrusted": {}})
        return finish(req, rows, reply)


def create(ctx: ProviderContext, transport: Transport | None = None) -> SearchConsole:
    return SearchConsole(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.ANALYTICS, NAME, ConnectionMode.HOSTED, create,
                      settings_schema=SearchConsoleSettings)
