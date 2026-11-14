"""Manual automation_hooks: no outbound hook; events stay in the core."""
from __future__ import annotations

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode


class ManualAutomationHooks(ManualProvider):
    capability = Capability.AUTOMATION_HOOKS

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.HookRequest)
        return Outcome(c.HookResult(delivered=False), title="No hook is connected",
                       details={"event": request.event.name.value}, done=True)


def create(ctx: ProviderContext) -> ManualAutomationHooks:
    return ManualAutomationHooks(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.AUTOMATION_HOOKS, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
