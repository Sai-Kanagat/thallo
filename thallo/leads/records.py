"""The lead desk's own tables (migration 0027). See the migration for what each row means."""
from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from thallo.contracts.enums import MessageChannel
from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class LeadDeskSetting(TenantRow):
    __table__ = "lead_desk_setting"
    play_code: str = Field(pattern=r"^[A-Z]{1,4}$")
    service_area: list[str] = Field(default_factory=list)
    stock: list[str] = Field(default_factory=list)
    supported_use_cases: list[str] = Field(default_factory=list)
    response_minutes: int = Field(default=60, ge=5, le=10080)
    acknowledgement_template_id: Id | None = None


class LeadAction(TenantRow):
    __table__ = "lead_action"
    lead_id: Id
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    touch_id: Id | None = None
    occurred_at: AwareDatetime


class SendState(StrEnum):
    APPROVED = "approved"  # the person tapped; the provider has not answered yet
    OPENED = "opened"  # a draft or click-to-chat link was handed back; the person sends it themselves
    SENT = "sent"  # the provider said sent, or the person confirmed they sent it
    FAILED = "failed"


class LeadMessageSend(TenantRow):
    """`id` is the message's id."""

    __table__ = "lead_message_send"
    channel: MessageChannel
    recipient: str
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consent_id: Id | None = None
    approved_by: Id
    approved_at: AwareDatetime
    state: SendState
    external_id: str | None = None
    detail: str | None = None  # never a secret, never a vendor body
    updated_at: AwareDatetime


__all__ = ["LeadAction", "LeadDeskSetting", "LeadMessageSend", "SendState"]
