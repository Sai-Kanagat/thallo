"""WP-14's own tables (migration 0031). See the migration for what each row means."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from thallo.contracts.enums import MessageChannel
from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class NurtureSequence(TenantRow):
    """An ordered list of hash-approved nurture templates with the waits between them, and the
    per-person rules: quiet hours (tenant local time), at most `max_per_week` messages in any 7 days,
    at least `min_gap_hours` between two messages."""

    __table__ = "nurture_sequence"
    name: str = Field(min_length=1, max_length=80)
    channel: MessageChannel
    steps: list[dict[str, Any]] = Field(default_factory=list)  # [{"template_id": id, "wait_hours": int}]
    quiet_start_hour: int = Field(default=20, ge=0, le=23)
    quiet_end_hour: int = Field(default=9, ge=0, le=23)
    max_per_week: int = Field(default=2, ge=1, le=7)
    min_gap_hours: int = Field(default=48, ge=1, le=720)
    active: bool = True


class EnrolmentState(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"
    DONE = "done"


class NurtureEnrolment(TenantRow):
    __table__ = "nurture_enrolment"
    lead_id: Id
    person_id: Id
    sequence_id: Id
    state: EnrolmentState = EnrolmentState.ACTIVE
    step: int = Field(default=0, ge=0)  # the next step to draft
    due_at: AwareDatetime
    last_touch_id: Id | None = None  # a newer touch on the lead is a reply: the sequence stops
    stop_reason: str | None = None
    updated_at: AwareDatetime


class NurtureStepDraft(TenantRow):
    """One message per enrolment step: the unique pair makes a second run a no-op."""

    __table__ = "nurture_step_draft"
    enrolment_id: Id
    step: int = Field(ge=0)
    message_id: Id


class CrmSyncRecord(TenantRow):
    """What was last pushed for one person to one CRM: the projection's hash and the CRM's id."""

    __table__ = "crm_sync_record"
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    person_id: Id
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_id: str | None = None
    erased: bool = False
    synced_at: AwareDatetime


__all__ = ["CrmSyncRecord", "EnrolmentState", "NurtureEnrolment", "NurtureSequence", "NurtureStepDraft"]
