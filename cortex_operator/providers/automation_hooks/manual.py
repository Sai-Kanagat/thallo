"""Manual automation_hooks: no outbound hook; events stay in the core."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome
from cortex_operator.connections.registry import ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode


class ManualAutomationHooks(ManualProvider):
    capability = Capability.AUTOMATION_HOOKS

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.HookRequest)
        return Outcome(c.HookResult(delivered=False), title="No hook is connected",
                       details={"event": request.event.name.value}, done=True)


def create(ctx: ProviderContext) -> ManualAutomationHooks:
    return ManualAutomationHooks(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.AUTOMATION_HOOKS, "manual", ConnectionMode.MANUAL, create)
