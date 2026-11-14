"""Manual storage: nothing is stored for the person; they keep the file."""
from __future__ import annotations

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode


class ManualStorage(ManualProvider):
    capability = Capability.STORAGE

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.StorageRequest)
        return Outcome(
            c.StorageResult(uri=f"manual://pending/{request.sha256}", sha256=request.sha256, content=None),
            title="Keep the file yourself" if request.action == "put" else "Provide the file",
            steps=("Keep the file somewhere you control and note where.",) if request.action == "put"
            else ("Upload the file with this hash.",),
            details={"action": request.action, "sha256": request.sha256})


def create(ctx: ProviderContext) -> ManualStorage:
    return ManualStorage(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.STORAGE, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
