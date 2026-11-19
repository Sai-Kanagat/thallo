"""The trust tables (migration 0030). `autonomy_state`, `incident` and `cost_ledger` are core
(0001); these rows hold what the ladder, the canaries, the breakers and the weekly audit need
beyond those counters."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field

from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class ItemOutcome(StrEnum):
    """One reviewed item. A defect is a rejection or an edit that changes meaning, a claim or
    anything a hard rule covers. A typo fix is logged and does not count."""

    CLEAN = "clean"
    TYPO = "typo"
    DEFECT = "defect"
    ESCAPED = "escaped"  # a defect found after approval
    HARD_RULE = "hard_rule"  # a hard-rule hit that reached approval


COUNTS_AS_DEFECT = frozenset({ItemOutcome.DEFECT, ItemOutcome.ESCAPED, ItemOutcome.HARD_RULE})


class CanaryFlaw(StrEnum):
    SWAPPED_CLAIM = "swapped_claim"  # a number swapped for a wrong one
    BANNED_PHRASE = "banned_phrase"
    OFF_BRAND_IMAGE = "off_brand_image"


class CanaryState(StrEnum):
    PLANTED = "planted"
    CAUGHT = "caught"  # the reviewer rejected it
    MISSED = "missed"  # the reviewer approved it


class AuditVerdict(StrEnum):
    PENDING = "pending"
    CORRECT = "correct"
    WRONG = "wrong"


ALL_PROVIDERS = "*"


class TrustTask(TenantRow):
    """Per task type: what the ladder needs beyond `autonomy_state`."""

    __table__ = "trust_task"
    __unique__ = (("tenant_id", "task_type"),)
    task_type: str = Field(min_length=1, max_length=80)
    version_key: str | None = None  # model id and prompt version the items were made with
    version_changed_at: AwareDatetime | None = None
    clean_since_version: int = 0  # clean items in a row since the last version change
    calibration_done: bool = False  # the calibration batch of 20
    voice_rules_signed_by: Id | None = None
    template_sha256: str | None = None
    template_signed_by: Id | None = None
    pause_reason: str | None = None
    updated_at: AwareDatetime


class TrustItem(TenantRow):
    __table__ = "trust_item"
    __unique__ = (("tenant_id", "task_type", "subject_id"),)
    task_type: str
    subject_kind: str
    subject_id: str
    outcome: ItemOutcome
    version_key: str | None = None
    note: str | None = None
    occurred_at: AwareDatetime


class TrustCanary(TenantRow):
    __table__ = "trust_canary"
    __unique__ = (("tenant_id", "variant_id"),)
    task_type: str
    variant_id: Id
    flaw: CanaryFlaw
    state: CanaryState = CanaryState.PLANTED
    planted_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    resolved_by: Id | None = None


class CostCap(TenantRow):
    """A per-run or per-day cap for this tenant, for all providers (`*`) or one."""

    __table__ = "cost_cap"
    __unique__ = (("tenant_id", "scope", "provider"),)
    scope: str  # BreakerScope value
    provider: str = ALL_PROVIDERS
    cap_eur: Decimal = Field(ge=0)
    set_by: str
    set_at: AwareDatetime


class BreakerTrip(TenantRow):
    __table__ = "breaker_trip"
    scope: str  # BreakerScope value
    subject: str  # tenant, provider:<name>, global, global:provider:<name>
    run_ref: str | None = None
    task_type: str | None = None
    day: date
    cap_eur: Decimal
    spent_eur: Decimal
    tripped_at: AwareDatetime
    reset_at: AwareDatetime | None = None
    reset_by: Id | None = None


class AuditSentence(TenantRow):
    """One of the week's 20 sentences checked by hand."""

    __table__ = "trust_audit_sentence"
    __unique__ = (("tenant_id", "week_start", "position"),)
    week_start: date
    position: int = Field(ge=1, le=20)
    variant_id: Id
    sentence: str = Field(min_length=1, max_length=2000)
    verdict: AuditVerdict = AuditVerdict.PENDING
    note: str | None = Field(default=None, max_length=500)
    checked_by: Id | None = None
    checked_at: AwareDatetime | None = None


__all__ = [
    "ALL_PROVIDERS", "COUNTS_AS_DEFECT", "AuditSentence", "AuditVerdict", "BreakerTrip", "CanaryFlaw",
    "CanaryState", "CostCap", "ItemOutcome", "TrustCanary", "TrustItem", "TrustTask",
]
