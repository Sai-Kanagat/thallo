"""Manual video: a person uploads a finished clip."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome, pending_media
from cortex_operator.connections.registry import ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode, MediaKind, MediaProvenance


class ManualVideo(ManualProvider):
    capability = Capability.VIDEO

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.VideoRequest)
        return Outcome(
            pending_media(request, MediaKind.VIDEO, "video/mp4", MediaProvenance.CLIENT_CLIP, request.width,
                          request.height, request.duration_ms),
            title="Upload the finished clip",
            steps=(f"Upload a {request.width}x{request.height} clip.", "Add captions before uploading."),
            details={"action": request.action, "script": request.script})


def create(ctx: ProviderContext) -> ManualVideo:
    return ManualVideo(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.VIDEO, "manual", ConnectionMode.MANUAL, create)
