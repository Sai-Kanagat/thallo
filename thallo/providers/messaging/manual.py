"""Manual messaging: returns a click-to-chat link for the seller's own app. It never sends anything."""
from __future__ import annotations

import re
from urllib.parse import quote

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, MessageChannel


class ManualMessaging(ManualProvider):
    capability = Capability.MESSAGING

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.MessageSendRequest)
        digits = re.sub(r"\D", "", request.to)
        url: str | None = None
        if request.channel is MessageChannel.WHATSAPP and len(digits) >= 6:
            url = f"https://wa.me/{digits}?text={quote(request.body)}"
        elif request.channel is MessageChannel.SMS and len(digits) >= 6:
            url = f"sms:{digits}?body={quote(request.body)}"
        elif request.channel is MessageChannel.EMAIL and "@" in request.to:
            url = f"mailto:{quote(request.to, safe='@')}?body={quote(request.body)}"
        return Outcome(
            c.MessageSendResult(status="manual_link", click_to_chat_url=url),
            title="Open the chat and send it yourself" if url else "The recipient address is not usable",
            steps=("Open the link, read the message, and press send yourself.",) if url
            else ("Fix the recipient's number or address on the lead.",),
            details={"channel": request.channel.value, "message_id": request.message_id})


def create(ctx: ProviderContext) -> ManualMessaging:
    return ManualMessaging(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.MESSAGING, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
