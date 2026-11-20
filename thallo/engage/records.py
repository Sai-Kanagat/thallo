"""The engagement inbox's one table (migration 0034). See the migration for what each column means."""
from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from thallo.contracts.enums import Channel
from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class Source(StrEnum):
    COMMENT = "comment"
    MESSAGE = "message"


class Category(StrEnum):
    QUESTION = "question"
    PRAISE = "praise"
    COMPLAINT = "complaint"
    LEAD = "lead"
    ABUSE = "abuse"
    CRISIS = "crisis"
    SPAM = "spam"
    OTHER = "other"


FLAGGED = frozenset({Category.ABUSE, Category.CRISIS})


class ItemState(StrEnum):
    FLAGGED = "flagged"  # abuse or crisis: a person looks first, no suggestion yet
    OPEN = "open"  # suggestion shown; a person copies it or replies in the platform
    HANDLED = "handled"  # a person says they replied in the platform
    HANDED_TO_LEADS = "handed_to_leads"
    DISMISSED = "dismissed"


class EngageItem(TenantRow):
    __table__ = "engage_item"
    channel: Channel
    source: Source
    external_id: str = Field(min_length=1, max_length=200)
    author: str | None = Field(default=None, max_length=80)  # untrusted
    text: str = Field(max_length=4000)  # untrusted
    permalink: str | None = None  # https only, checked on the way in
    received_at: AwareDatetime
    category: Category
    reasons: list[str] = Field(default_factory=list)
    state: ItemState
    suggested_reply: str | None = Field(default=None, max_length=1500)
    suggestion_model: str | None = None
    prompt_version: str | None = None
    acknowledged_by: Id | None = None  # the person who looked at a flagged item
    handled_by: Id | None = None
    lead_id: Id | None = None
    updated_at: AwareDatetime


__all__ = ["FLAGGED", "Category", "EngageItem", "ItemState", "Source"]
