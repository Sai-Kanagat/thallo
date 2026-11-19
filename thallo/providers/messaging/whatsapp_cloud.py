"""WhatsApp Cloud API (Meta). Sends one text message, only with a person's tap and the operator's
live switch on (see live_base.py). Otherwise the manual link opens the seller's own WhatsApp.

API, from Meta's messages reference (read 19 Nov 2026; test fixtures are hand-written from it):
    POST https://graph.facebook.com/{version}/{phone_number_id}/messages
    Authorization: Bearer <system user token>
    {"messaging_product": "whatsapp", "recipient_type": "individual", "to": "<digits>", "type": "text",
     "text": {"preview_url": false, "body": "..."}}
    reply {"messaging_product": "whatsapp", "contacts": [{"input", "wa_id"}], "messages": [{"id", ...}]}

Not verified here: free text reaches a person only inside the 24-hour window after they last wrote;
outside it Meta requires an approved WhatsApp template, which this provider does not send. A
refusal for that reason comes back as a 4xx and is recorded as not sent.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, MessageChannel
from thallo.providers.messaging.live_base import LiveMessagingProvider, MessageOutcomeUnknown, post_json
from thallo.providers.text.base import Auth

NAME = "whatsapp_cloud"
ORIGIN = "https://graph.facebook.com"


class WhatsAppSettings(BaseModel):
    """Tenant-writable: ids only, no URL, host or credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    phone_number_id: str = Field(pattern=r"^\d{5,20}$")
    api_version: str = Field(default="v21.0", pattern=r"^v\d{1,2}\.\d$")


class WhatsAppCloud(LiveMessagingProvider):
    name = NAME
    channels = frozenset({MessageChannel.WHATSAPP})
    settings_model = WhatsAppSettings
    licence_note = "Meta WhatsApp Business terms; per-conversation charges billed by Meta"

    def send(self, request: c.MessageSendRequest) -> str:
        to = re.sub(r"\D", "", request.to)
        url = f"{ORIGIN}/{self.settings.api_version}/{self.settings.phone_number_id}/messages"
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
                   "text": {"preview_url": False, "body": request.body}}
        body = post_json(self, url, payload, Auth("authorization", self.secret(), "Bearer "))
        msgs = body.get("messages") if isinstance(body, dict) else None
        mid = msgs[0].get("id") if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict) else None
        if not isinstance(mid, str) or not mid:
            raise MessageOutcomeUnknown("WhatsApp accepted the call but returned no message id")
        return mid


def create(ctx: ProviderContext, transport=None, environ: Mapping[str, str] | None = None) -> WhatsAppCloud:  # noqa: ANN001
    return WhatsAppCloud(ctx, transport=transport, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.MESSAGING, NAME, ConnectionMode.HOSTED, create, settings_schema=WhatsAppSettings)
