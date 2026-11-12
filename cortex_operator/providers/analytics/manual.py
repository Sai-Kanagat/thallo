"""Manual analytics: a person uploads a CSV or types the numbers. Nothing is invented; metrics come back missing."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome
from cortex_operator.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode


class ManualAnalytics(ManualProvider):
    capability = Capability.ANALYTICS

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.AnalyticsRequest)
        return Outcome(
            c.AnalyticsResult(rows=(), missing=request.metrics),
            title="Enter the numbers",
            steps=("Upload a CSV or type each metric for the period.",),
            details={"source": request.source, "metrics": list(request.metrics),
                     "start": request.start.isoformat(), "end": request.end.isoformat()})


def create(ctx: ProviderContext) -> ManualAnalytics:
    return ManualAnalytics(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.ANALYTICS, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
