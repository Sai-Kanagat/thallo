"""Event names and payloads.

Events record what happened. They are written to event_log (the outbox) in the same
transaction as the state change. Automation hooks may read them; nothing outside the
core may emit one, and no event can cause a publish, send or spend on its own.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator  # noqa: F401

from cortex_operator.contracts.actors import Actor, ActorRef
from cortex_operator.contracts.enums import (
    ApprovalSubject,
    AutonomyLevel,
    BreakerScope,
    Capability,
    LeadRoute,
    OutcomeKind,
    RejectionReason,
    TouchKind,
)


class EventName(StrEnum):
    CLAIM_CANDIDATE_ADDED = "claim.candidate_added"
    CLAIM_VERIFIED = "claim.verified"
    CLAIM_CHANGED = "claim.changed"
    CLAIM_EXPIRED = "claim.expired"
    IDEA_PROPOSED = "idea.proposed"
    FORECAST_SEALED = "forecast.sealed"
    FORECAST_SCORED = "forecast.scored"
    BUNDLE_READY = "bundle.ready_for_review"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_VOIDED = "approval.voided"
    APPROVAL_EXPIRED = "approval.expired"
    VARIANT_REJECTED = "variant.rejected"
    EDIT_RECORDED = "edit.recorded"
    PUBLICATION_SENT = "publication.sent"
    PUBLICATION_READ_BACK = "publication.read_back"
    PUBLICATION_MISMATCH = "publication.mismatch"
    PUBLICATION_BLOCKED = "publication.blocked"
    SLOT_MISSED = "slot.missed"
    SLOT_BENCHED = "slot.benched"
    METRICS_PULLED = "metrics.pulled"
    TOUCH_RECORDED = "touch.recorded"
    LEAD_CREATED = "lead.created"
    LEAD_ROUTED = "lead.routed"
    LEAD_OUTCOME = "lead.outcome"
    RULE_PROPOSED = "rule.proposed"
    SPEND_PROPOSED = "spend.proposed"
    SPEND_SIGNED = "spend.signed"
    AD_PAUSED = "ad.paused"
    CANARY_PLANTED = "canary.planted"
    CANARY_CAUGHT = "canary.caught"
    CANARY_MISSED = "canary.missed"
    AUTONOMY_CHANGED = "autonomy.changed"
    BREAKER_TRIPPED = "breaker.tripped"
    CONNECTION_HEALTH_CHANGED = "connection.health_changed"
    TOKEN_EXPIRING = "token.expiring"
    KILL_SWITCH = "kill_switch.changed"


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ClaimPayload(Payload):
    claim_id: str
    code: str


class ClaimChangedPayload(ClaimPayload):
    voided_approval_ids: list[str] = Field(default_factory=list)


class IdPayload(Payload):
    id: str


class ForecastScoredPayload(Payload):
    forecast_id: str
    beat_baseline: bool | None


class BundlePayload(Payload):
    bundle_id: str
    root_hash: str


class ApprovalPayload(Payload):
    approval_id: str
    subject_kind: ApprovalSubject
    subject_id: str
    content_hash: str


class ApprovalVoidedPayload(ApprovalPayload):
    reason: str


class RejectedPayload(Payload):
    variant_id: str
    reason: RejectionReason


class PublicationPayload(Payload):
    publication_id: str
    variant_id: str


class PublicationMismatchPayload(PublicationPayload):
    diff: str


class BlockedPayload(Payload):
    variant_id: str
    reason: str


class SlotPayload(Payload):
    slot_ref: str
    variant_id: str | None = None


class MetricsPayload(Payload):
    publication_id: str
    day: int
    missing: list[str] = Field(default_factory=list)


class TouchPayload(Payload):
    touch_id: str
    kind: TouchKind


class LeadPayload(Payload):
    lead_id: str


class LeadRoutedPayload(LeadPayload):
    route: LeadRoute
    fit: int
    heat: float


class LeadOutcomePayload(LeadPayload):
    outcome: OutcomeKind


class SpendPayload(Payload):
    spend_proposal_id: str


class AdPausedPayload(Payload):
    ad_id: str
    rule_fired: str


class CanaryPayload(Payload):
    variant_id: str
    task_type: str


class AutonomyPayload(Payload):
    task_type: str
    from_level: AutonomyLevel
    to_level: AutonomyLevel
    reason: str


class BreakerPayload(Payload):
    scope: BreakerScope
    cap_eur: str
    spent_eur: str
    subject: str


class ConnectionHealthPayload(Payload):
    connection_id: str
    capability: Capability
    connected: bool


class TokenExpiringPayload(Payload):
    channel_account_id: str
    expires_at: AwareDatetime


class KillSwitchPayload(Payload):
    scope: str  # brand or global
    paused: bool


PAYLOADS: dict[EventName, type[Payload]] = {
    EventName.CLAIM_CANDIDATE_ADDED: ClaimPayload,
    EventName.CLAIM_VERIFIED: ClaimPayload,
    EventName.CLAIM_CHANGED: ClaimChangedPayload,
    EventName.CLAIM_EXPIRED: ClaimPayload,
    EventName.IDEA_PROPOSED: IdPayload,
    EventName.FORECAST_SEALED: IdPayload,
    EventName.FORECAST_SCORED: ForecastScoredPayload,
    EventName.BUNDLE_READY: BundlePayload,
    EventName.APPROVAL_GRANTED: ApprovalPayload,
    EventName.APPROVAL_VOIDED: ApprovalVoidedPayload,
    EventName.APPROVAL_EXPIRED: ApprovalPayload,
    EventName.VARIANT_REJECTED: RejectedPayload,
    EventName.EDIT_RECORDED: IdPayload,
    EventName.PUBLICATION_SENT: PublicationPayload,
    EventName.PUBLICATION_READ_BACK: PublicationPayload,
    EventName.PUBLICATION_MISMATCH: PublicationMismatchPayload,
    EventName.PUBLICATION_BLOCKED: BlockedPayload,
    EventName.SLOT_MISSED: SlotPayload,
    EventName.SLOT_BENCHED: SlotPayload,
    EventName.METRICS_PULLED: MetricsPayload,
    EventName.TOUCH_RECORDED: TouchPayload,
    EventName.LEAD_CREATED: LeadPayload,
    EventName.LEAD_ROUTED: LeadRoutedPayload,
    EventName.LEAD_OUTCOME: LeadOutcomePayload,
    EventName.RULE_PROPOSED: IdPayload,
    EventName.SPEND_PROPOSED: SpendPayload,
    EventName.SPEND_SIGNED: SpendPayload,
    EventName.AD_PAUSED: AdPausedPayload,
    EventName.CANARY_PLANTED: CanaryPayload,
    EventName.CANARY_CAUGHT: CanaryPayload,
    EventName.CANARY_MISSED: CanaryPayload,
    EventName.AUTONOMY_CHANGED: AutonomyPayload,
    EventName.BREAKER_TRIPPED: BreakerPayload,
    EventName.CONNECTION_HEALTH_CHANGED: ConnectionHealthPayload,
    EventName.TOKEN_EXPIRING: TokenExpiringPayload,
    EventName.KILL_SWITCH: KillSwitchPayload,
}


class Event(BaseModel):
    """The envelope. `payload` is validated against PAYLOADS[name]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    tenant_id: str
    name: EventName
    version: int = 1
    occurred_at: AwareDatetime
    actor: ActorRef  # kind and id only: a session never enters an event
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _check_payload(self) -> Event:
        PAYLOADS[self.name].model_validate(self.payload)
        return self

    @classmethod
    def build(cls, *, id: str, tenant_id: str, occurred_at: AwareDatetime, actor: Actor | ActorRef,
              payload: Payload, name: EventName) -> Event:
        expected = PAYLOADS[name]
        if not isinstance(payload, expected):
            raise TypeError(f"{name.value} needs {expected.__name__}, got {type(payload).__name__}")
        if getattr(actor, "tenant_id", tenant_id) != tenant_id:
            raise ValueError("event tenant and actor tenant differ")
        return cls(id=id, tenant_id=tenant_id, name=name, occurred_at=occurred_at, actor=ActorRef.of(actor),
                   payload=payload.model_dump(mode="json"))
