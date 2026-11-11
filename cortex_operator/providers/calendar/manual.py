"""Manual calendar: the built-in week plan is the calendar, so nothing is mirrored out."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome
from cortex_operator.connections.registry import ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode


class ManualCalendar(ManualProvider):
    capability = Capability.CALENDAR

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.CalendarRequest)
        return Outcome(c.CalendarResult(mirrored=0), title="Use the built-in week plan",
                       details={"slots": len(request.slots)}, done=True)


def create(ctx: ProviderContext) -> ManualCalendar:
    return ManualCalendar(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.CALENDAR, "manual", ConnectionMode.MANUAL, create)
