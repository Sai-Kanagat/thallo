"""Manual image: a person uploads a photo or a finished graphic."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome, pending_media
from cortex_operator.connections.registry import ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode, MediaKind, MediaProvenance


class ManualImage(ManualProvider):
    capability = Capability.IMAGE

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.ImageRequest)
        prov = MediaProvenance.CLIENT_PHOTO if request.purpose == "upload" else MediaProvenance.CLIENT_GRAPHIC
        return Outcome(
            pending_media(request, MediaKind.IMAGE, "image/png", prov, request.width, request.height),
            title="Upload the image",
            steps=(f"Upload a {request.width}x{request.height} image.", "Say where it came from."),
            details={"purpose": request.purpose, "width": request.width, "height": request.height})


def create(ctx: ProviderContext) -> ManualImage:
    return ManualImage(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.IMAGE, "manual", ConnectionMode.MANUAL, create)
