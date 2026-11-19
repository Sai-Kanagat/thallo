"""Resend (email API). Sends one plain-text email, only with a person's tap and the operator's live
switch on (see live_base.py). Otherwise the manual mailto link opens the seller's own mailbox.

API, from resend.com/docs/api-reference/emails/send-email (read 19 Nov 2026; fixtures hand-written):
    POST https://api.resend.com/emails
    Authorization: Bearer <api key>
    {"from": "...", "to": ["..."], "subject": "...", "text": "..."}   reply {"id": "..."}
Resend also takes an `Idempotency-Key` header. The shared transport sends fixed headers only, so it
is not sent; the lead desk's one-tap record is what stops a second send.
"""
from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, MessageChannel
from thallo.providers.messaging.live_base import LiveMessagingProvider, MessageOutcomeUnknown, post_json
from thallo.providers.text.base import Auth

NAME = "resend"
URL = "https://api.resend.com/emails"
EMAIL = r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$"


class EmailSettings(BaseModel):
    """Tenant-writable: the sender address and a subject line. No URL, host or credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    from_address: str = Field(pattern=EMAIL)
    from_name: str | None = Field(default=None, pattern=r"^[^\r\n<>\"]{1,60}$")
    subject: str = Field(default="Your enquiry", pattern=r"^[^\r\n]{1,120}$")


def sender_line(s: EmailSettings) -> str:
    return f"{s.from_name} <{s.from_address}>" if s.from_name else s.from_address


class Resend(LiveMessagingProvider):
    name = NAME
    channels = frozenset({MessageChannel.EMAIL})
    settings_model = EmailSettings
    licence_note = "Resend terms; billed by Resend per email"

    def send(self, request: c.MessageSendRequest) -> str:
        payload = {"from": sender_line(self.settings), "to": [request.to.strip()], "subject": self.settings.subject,
                   "text": request.body}
        body = post_json(self, URL, payload, Auth("authorization", self.secret(), "Bearer "))
        mid = body.get("id") if isinstance(body, dict) else None
        if not isinstance(mid, str) or not mid:
            raise MessageOutcomeUnknown("Resend accepted the call but returned no email id")
        return mid


def create(ctx: ProviderContext, transport=None, environ: Mapping[str, str] | None = None) -> Resend:  # noqa: ANN001
    return Resend(ctx, transport=transport, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.MESSAGING, NAME, ConnectionMode.HOSTED, create, settings_schema=EmailSettings)
