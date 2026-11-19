"""The learning loop's own tables (migration 0032). Shared tables (`edit`, `rule_proposal`,
`voice_rule`, `prompt_version`, `approval`) are used as the contracts define them."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from thallo.contracts.enums import Channel, Language, RuleScope
from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow

SHA = r"^[0-9a-f]{64}$"


class CaseKind(StrEnum):
    REGRESSION = "regression"  # a real edit: the draft and the human's final
    GOLDEN = "golden"  # a hand-picked brief and the piece the client signed off


class RegressionCase(TenantRow):
    """One case in a client's set. Pilot material lives only here, tenant-scoped, never in `evals/`."""

    __table__ = "regression_case"
    kind: CaseKind
    edit_id: Id | None = None
    rule_proposal_id: Id | None = None
    scope: RuleScope = RuleScope.BRAND
    scope_ref: str | None = None
    channel: Channel | None = None
    language: Language | None = None
    brief: dict[str, Any] = Field(default_factory=dict)
    draft: str | None = None
    final: str = Field(min_length=1)


class ChangeKind(StrEnum):
    PROMPT = "prompt"
    TEMPLATE = "template"
    MODEL = "model"


class EvalRun(TenantRow):
    """One run of one change against one client's case set. Bound to the exact change and set."""

    __table__ = "eval_run"
    change_kind: ChangeKind
    change_name: str = Field(min_length=1)
    change_sha256: str = Field(pattern=SHA)
    case_set_sha256: str = Field(pattern=SHA)
    cases: int = Field(ge=0)
    failed: int = Field(ge=0)
    flagged: int = Field(ge=0)
    not_run: int = Field(ge=0)
    passed: bool
    detail: list[dict[str, Any]] = Field(default_factory=list)
    ran_at: AwareDatetime


class BlindSource(StrEnum):
    THALLO = "thallo"
    AGENCY = "agency"


class BlindItem(TenantRow):
    """A piece in a blind session. `source` is never sent to a rater before they finish scoring."""

    __table__ = "blind_item"
    session_id: Id
    opaque_ref: str = Field(pattern=r"^[0-9a-f]{16}$")
    position: int = Field(ge=0)
    source: BlindSource
    brief_ref: str
    text: str


class BlindScore(TenantRow):
    __table__ = "blind_score"
    session_id: Id
    item_id: Id
    rater_id: Id
    brand_fit: int = Field(ge=1, le=5)
    craft: int = Field(ge=1, le=5)
    specificity: int = Field(ge=1, le=5)
    would_post: int = Field(ge=1, le=5)
    scored_at: AwareDatetime


__all__ = ["BlindItem", "BlindScore", "BlindSource", "CaseKind", "ChangeKind", "EvalRun", "RegressionCase"]
