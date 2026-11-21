"""The one table this package adds (migration 0038). Not part of `thallo.contracts`.

`decision_proposal`: one monthly proposal (keep, change, stop) or the month's "no decision", with
the evidence it rests on, the exact change, and the hash a person signs. Signing records the
decision; applying a pillar share change is the growth model's job.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class DecisionKind(StrEnum):
    KEEP = "keep"
    CHANGE = "change"
    STOP = "stop"
    NO_DECISION = "no_decision"


class DecisionState(StrEnum):
    PROPOSED = "proposed"
    SIGNED = "signed"
    DECLINED = "declined"
    NOTED = "noted"  # a "no decision this month" row: nothing to sign


class DecisionProposal(TenantRow):
    __table__ = "decision_proposal"
    __unique__ = (("tenant_id", "period", "dimension", "subject"),)
    period: str = Field(pattern=r"^\d{4}-\d{2}$")  # calendar month, UTC
    dimension: str = Field(pattern=r"^(\*|pillar|channel|format)$")
    subject: str = Field(min_length=1, max_length=120)  # pillar id, channel or format; "*" for none
    kind: DecisionKind
    state: DecisionState
    summary: str = Field(min_length=1, max_length=400)
    evidence: dict[str, Any] = Field(default_factory=dict)
    change: dict[str, Any] = Field(default_factory=dict)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_by: Id | None = None
    signed_at: AwareDatetime | None = None
    declined_by: Id | None = None
    declined_at: AwareDatetime | None = None


__all__ = ["DecisionKind", "DecisionProposal", "DecisionState"]
