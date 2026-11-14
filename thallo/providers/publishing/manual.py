"""Manual publishing: the system prepares the exact approved copy pack, a person posts it and confirms the live link.

Nothing is ever published from here."""
from __future__ import annotations

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode


class ManualPublishing(ManualProvider):
    capability = Capability.PUBLISHING

    def fulfil(self, request: c.Request) -> Outcome:
        if isinstance(request, c.ReadbackRequest):
            return Outcome(
                c.ReadbackResult(live_text=None, media_changed=None),
                title="Confirm what is live",
                steps=("Open the live post.", "Paste its text and say whether the media changed."),
                details={"publication_id": request.publication_id})
        assert isinstance(request, c.PublishRequest)
        pack = {
            "channel": request.channel.value, "text": request.text, "media_sha256": list(request.media_sha256),
            "scheduled_for": request.scheduled_for.isoformat() if request.scheduled_for else None,
            "content_hash": request.content_hash, "approval_id": request.approval_id,
            "variant_id": request.variant_id,
        }
        return Outcome(
            c.PublishResult(status="manual_pending", copy_pack=pack),
            title=f"Post this on {request.channel.value}",
            steps=("Post exactly this text and these files, unedited.", "Paste the live link back to confirm."),
            details={"variant_id": request.variant_id})


def create(ctx: ProviderContext) -> ManualPublishing:
    return ManualPublishing(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.PUBLISHING, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
