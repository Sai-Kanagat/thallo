"""Manual crm: the built-in lead desk is the CRM; a person moves records by CSV."""
from __future__ import annotations

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode


class ManualCrm(ManualProvider):
    capability = Capability.CRM

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.CrmSyncRequest)
        return Outcome(
            c.CrmSyncResult(synced=0),
            title="Export the lead desk as CSV",
            steps=("Export the records as CSV.", "Import the file into the other system by hand."),
            details={"records": len(request.records)})


def create(ctx: ProviderContext) -> ManualCrm:
    return ManualCrm(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.CRM, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
