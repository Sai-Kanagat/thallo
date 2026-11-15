"""The publisher's own table (migration 0022): where one publication's send stands.

`publication` (core, one row per variant) has no state column, and "in flight", "sent but the
outcome is unknown" and "waiting for a person" all look the same there (no `sent_at`, no
`external_id`). So the publisher keeps one `publication_send` row per publication, with the same
id, written in the same transaction as the publication. No shared table changes.

States (`SendState`):
  sending         claimed and committed; the provider call is about to run or running.
  sent            the provider accepted it (an external id or a live link came back).
  manual_pending  the manual pack was prepared; a person posts it and confirms the live link.
  retry           definitely not sent (refused before it left, or a 4xx or 429 that means no
                  post exists); may be tried again with the same bytes after `next_try_at`.
  failed          not sent, and not to be retried by the machine (non-retryable, or out of tries).
  needs_check     the send may or may not have landed (timeout, reset, 5xx, a crash before the
                  outcome was written), or a reconcile could not tell. Never resent by the
                  machine; reconciled by readback, or settled by a person.
A `sending` row older than the lease counts as `needs_check`: the process that claimed it died.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from thallo.contracts.models import TenantRow


class SendState(StrEnum):
    SENDING = "sending"
    SENT = "sent"
    MANUAL_PENDING = "manual_pending"
    RETRY = "retry"
    FAILED = "failed"
    NEEDS_CHECK = "needs_check"


class PublicationSend(TenantRow):
    """`id` is the publication's id."""

    __table__ = "publication_send"
    state: SendState
    attempts: int = Field(default=1, ge=1)
    requested_at: AwareDatetime  # the date given to the provider; the centre of a reconcile lookup
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # sha256 of the UTF-8 text sent
    media_sha256: list[str] = Field(default_factory=list)
    next_try_at: AwareDatetime | None = None
    detail: str | None = None  # never a secret, never a vendor body
    updated_at: AwareDatetime


__all__ = ["PublicationSend", "SendState"]
