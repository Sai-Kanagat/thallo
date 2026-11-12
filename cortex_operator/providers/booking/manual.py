"""Manual booking: a person offers callback slots by hand."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome
from cortex_operator.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode


class ManualBooking(ManualProvider):
    capability = Capability.BOOKING

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.BookingRequest)
        return Outcome(
            c.BookingResult(link=None, slots=()),
            title="Offer callback slots",
            steps=("Pick two or three times that suit you.", "Offer them to the lead and record the one chosen."),
            details={"lead_id": request.lead_id, "kind": request.kind})


def create(ctx: ProviderContext) -> ManualBooking:
    return ManualBooking(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.BOOKING, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
