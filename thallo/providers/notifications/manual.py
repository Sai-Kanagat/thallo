"""Manual notifications: no outside notification; a person looks at the review desk."""
from __future__ import annotations

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode


class ManualNotifications(ManualProvider):
    capability = Capability.NOTIFICATIONS

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.NotifyRequest)
        return Outcome(
            c.NotifyResult(delivered=False),
            title="Tell the user in person", steps=("Open the review desk.",),
            details={"user_id": request.user_id, "title": request.title})


def create(ctx: ProviderContext) -> ManualNotifications:
    return ManualNotifications(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.NOTIFICATIONS, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
