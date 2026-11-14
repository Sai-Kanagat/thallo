"""Manual booking: a person offers callback slots by hand."""
from __future__ import annotations

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode


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
