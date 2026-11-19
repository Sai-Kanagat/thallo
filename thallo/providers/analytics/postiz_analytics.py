"""Scheduler analytics: Postiz platform analytics (docs.postiz.com/public-api/analytics/platform,
read 19 Nov 2026; fixtures are hand-written from that page, not captured from a live server).

    GET https://api.postiz.com/public/v1/analytics/{integration}?date=<days back>
    header `Authorization: <api key>` (no "Bearer"); 30 requests an hour
    reply [{label: "Followers", data: [{total: "900", date: "YYYY-MM-DD"}], percentageChange}]

`label` is the source's text: it is mapped through LABELS (case-folded) and never used as a
metric name itself; an unknown label is ignored. `total` is a string; one that is not a number is
missing. Some platforms answer an empty list: every metric is then missing, never zero.
Postiz Cloud only; self-hosted Postiz analytics is not wired yet.
"""
from __future__ import annotations

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

NAME = "postiz_analytics"
ORIGIN = ("https", "api.postiz.com", 443)
LABELS = {"followers": "followers", "impressions": "impressions", "likes": "likes", "comments": "comments",
          "shares": "shares", "reach": "reach", "views": "views", "clicks": "clicks"}


class PostizAnalyticsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    integration_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, le=120)


class PostizAnalytics(AnalyticsHttpProvider):
    name = NAME
    origins = (ORIGIN,)
    settings_model = PostizAnalyticsSettings
    metrics_known = tuple(LABELS.values())
    licence_note = "Postiz public API, the tenant's connected channel"

    def _pull(self, req: c.AnalyticsRequest) -> c.AnalyticsResult:
        days = (self.clock.now().date() - req.start).days + 1
        days = min(max(days, 1), 365)
        url = f"https://api.postiz.com/public/v1/analytics/{self.settings.integration_id}?date={days}"
        reply = self._json(RawCall("GET", url, auth=self._header_auth("authorization", ""),
                                   timeout=self._timeout()), "the Postiz analytics call")
        raw_ref(reply)  # kept even when the shape check below fails
        if not isinstance(reply, list):
            raise PullFailed("the Postiz analytics reply has an unexpected shape")
        rows: list[dict[str, Any]] = []
        for series in reply:
            if not isinstance(series, dict) or not isinstance(series.get("label"), str):
                continue
            metric = LABELS.get(series["label"].strip().casefold())
            data = series.get("data")
            if metric is None or not isinstance(data, list):
                continue
            for point in data:
                if not isinstance(point, dict) or not isinstance(point.get("date"), str):
                    continue
                n = number(point.get("total"))
                if n is not None:
                    rows.append({"date": point["date"][:10], "metric": metric, "value": n, "untrusted": {}})
        return finish(req, rows, reply)


def create(ctx: ProviderContext, transport: Transport | None = None) -> PostizAnalytics:
    return PostizAnalytics(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.ANALYTICS, NAME, ConnectionMode.HOSTED, create,
                      settings_schema=PostizAnalyticsSettings)
