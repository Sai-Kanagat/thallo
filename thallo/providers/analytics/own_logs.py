"""Our own logs: counts per day from this tenant's `touch` table (WP-12), read through the reader
the intake stages (thallo/signals/touches.py). No outside call.

Metric names map to touch kinds. A day with no touch of a kind is a true zero here, because the
log is complete for our own redirector and capture points: the reader covers every day in the
window. A metric we do not log is missing.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import ClassVar

from thallo.connections.errors import InvalidConnection, ProviderUnavailable
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider
from thallo.providers.analytics.http_base import finish
from thallo.signals import context as staging

NAME = "own_logs"
METRICS = {"link_clicks": "link", "whatsapp_codes": "whatsapp_code", "forms": "form", "calls": "call",
           "walk_ins": "walk_in_code", "self_reports": "self_report"}


class OwnLogs:
    capability: ClassVar[Capability] = Capability.ANALYTICS
    name = NAME
    mode = ConnectionMode.LOCAL

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self.clock = ctx.clock

    def _reader(self):
        s = staging.current()
        if s is None or s.tenant_id != self.ctx.tenant_id:
            return None
        return s.touches

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=NAME, mode=self.mode,
                              limits={"metrics": sorted(METRICS)}, licence_note="our own touch log",
                              sends_externally=False)

    def health(self) -> c.Health:
        ok = self._reader() is not None
        return c.Health(connected=ok, checked_at=self.clock.now(),
                        detail=None if ok else "no touch log reader is staged")

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")
        if self._reader() is None:
            raise ProviderUnavailable("no touch log reader is staged")
        return c.Estimate(cost_eur=Decimal(0), seconds=0.2)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.AnalyticsRequest)
        reader = self._reader()
        assert reader is not None
        counts = {(d.isoformat(), k): n for d, k, n in reader(request.start, request.end)}
        rows = []
        day, last = request.start, min(request.end, self.clock.now().date())
        while day <= last:  # a day still to come is not a zero
            for metric, kind in METRICS.items():
                if metric in request.metrics:
                    rows.append({"date": day.isoformat(), "metric": metric,
                                 "value": float(counts.get((day.isoformat(), kind), 0)), "untrusted": {}})
            day += timedelta(days=1)
        raw = [[d, k, n] for (d, k), n in sorted(counts.items())]
        return c.RunResult(output=finish(request, rows, raw), cost_eur=Decimal(0),
                           provenance=c.Provenance(made_by="provider", provider=NAME, mode=self.mode,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{NAME}")


def create(ctx: ProviderContext) -> OwnLogs:
    return OwnLogs(ctx)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.ANALYTICS, NAME, ConnectionMode.LOCAL, create, settings_schema=NoSettings)
