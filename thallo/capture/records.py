"""The capture tables (migration 0026). What people said lives on `source`; these rows point at it."""
from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, Field

from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class AnswerMode(StrEnum):
    QUESTION = "question"  # a recorded answer to one of the week's three questions
    INTERVIEW = "interview"  # the optional long interview, no question
    TYPED = "typed"  # typed instead of recorded (no microphone, or the person prefers it)


class TranscriptState(StrEnum):
    PENDING = "pending"  # recorded, not transcribed yet
    DONE = "done"
    NEEDS_TYPING = "needs_typing"  # the manual floor served: a person types what was said
    FAILED = "failed"  # the provider failed; retry or type it


class ShotState(StrEnum):
    OPEN = "open"
    FULFILLED = "fulfilled"
    DROPPED = "dropped"


class CaptureQuestion(TenantRow):
    __table__ = "capture_question"
    __unique__ = (("tenant_id", "week_start", "position"),)
    week_start: date
    position: int = Field(ge=1, le=3)
    play_code: str | None = None
    text: str = Field(min_length=1, max_length=500)


class CaptureAnswer(TenantRow):
    __table__ = "capture_answer"
    question_id: Id | None = None
    mode: AnswerMode
    source_id: Id
    media_id: Id | None = None
    consent_id: Id | None = None
    answered_by: Id
    transcript_state: TranscriptState
    provider: str | None = None
    detail: str | None = None  # never a vendor body or a secret
    candidates_extracted: bool = False
    updated_at: AwareDatetime


class ShotRequest(TenantRow):
    __table__ = "shot_request"
    __unique__ = (("tenant_id", "week_start", "position"),)
    week_start: date
    position: int = Field(ge=1, le=10)
    text: str = Field(min_length=1, max_length=500)
    people_in_shot: bool = False
    state: ShotState = ShotState.OPEN
    media_id: Id | None = None
    consent_id: Id | None = None
    fulfilled_by: Id | None = None
    fulfilled_at: AwareDatetime | None = None


__all__ = ["AnswerMode", "CaptureAnswer", "CaptureQuestion", "ShotRequest", "ShotState", "TranscriptState"]
