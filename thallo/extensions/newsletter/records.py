"""The newsletter tables (migration 0040)."""
from __future__ import annotations

from datetime import date

from pydantic import AwareDatetime, Field

from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow
from thallo.leads.records import SendState


class NewsletterIssue(TenantRow):
    __table__ = "newsletter_issue"
    period_start: date
    period_end: date
    variant_id: Id
    piece_brief_ids: list[Id] = Field(min_length=1)


class NewsletterSubscriber(TenantRow):
    __table__ = "newsletter_subscriber"
    person_id: Id
    consent_id: Id
    recipient: str
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    unsubscribed_at: AwareDatetime | None = None


class NewsletterDelivery(TenantRow):
    __table__ = "newsletter_delivery"
    issue_id: Id
    subscriber_id: Id
    recipient: str
    consent_id: Id
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: Id
    approved_at: AwareDatetime
    state: SendState
    external_id: str | None = None
    detail: str | None = None
    updated_at: AwareDatetime


__all__ = ["NewsletterDelivery", "NewsletterIssue", "NewsletterSubscriber", "SendState"]
