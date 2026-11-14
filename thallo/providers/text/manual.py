"""Manual text: a person writes or pastes the copy."""
from __future__ import annotations

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode


class ManualText(ManualProvider):
    capability = Capability.TEXT

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.TextRequest)
        return Outcome(
            c.TextResult(text="", data={"awaiting": "pasted_copy", "role": request.role.value}),
            title=f"Write the {request.role.value} copy yourself",
            steps=("Write or paste the copy for this role.", "Check every number against a verified claim."),
            details={"role": request.role.value, "instructions": request.instructions})


def create(ctx: ProviderContext) -> ManualText:
    return ManualText(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TEXT, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
