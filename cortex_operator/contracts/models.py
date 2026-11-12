"""Typed row models: one class per table. db/migrations/0001_core.sql is generated from these.

Conventions
- Every row carries `id`, `tenant_id` and `created_at` (Tenant itself has no tenant_id).
- Rows a model wrote also carry `model_id` and `prompt_version` (ModelMade).
- Money is Decimal in EUR. Times are timezone-aware UTC. No IP address is stored anywhere.
- Secrets never appear in a row. Rows hold a `secret_ref` the vault (WP-02) resolves.
- `fk("table")` marks a foreign key, `unique()` a unique column, `index()` an index.
  Composite uniques live in `__unique__`.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from cortex_operator.contracts import states as st
from cortex_operator.contracts.enums import (
    ActorKind,
    ApprovalMethod,
    ApprovalSubject,
    AutonomyLevel,
    CampaignRole,
    Capability,
    Channel,
    CheckLayer,
    CheckVerdict,
    ClaimKind,
    ConnectionMode,
    ConsentBasis,
    EditKind,
    FunnelStage,
    IncidentSeverity,
    Language,
    LeadRoute,
    LossReason,
    MediaKind,
    MediaProvenance,
    MessageChannel,
    MessageKind,
    MetricDay,
    OutcomeKind,
    RejectionReason,
    RuleScope,
    SourceKind,
    TextRole,
    TouchKind,
    UserRole,
)
from cortex_operator.contracts.ids import Id


def fk(table: str, *, unique: bool = False, **kw: Any) -> Any:
    extra: dict[str, Any] = {"fk": table}
    if unique:
        extra["unique"] = True
    return Field(json_schema_extra=extra, **kw)


def unique(**kw: Any) -> Any:
    return Field(json_schema_extra={"unique": True}, **kw)


def index(**kw: Any) -> Any:
    return Field(json_schema_extra={"index": True}, **kw)


Money = Decimal


class Row(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)
    __table__: ClassVar[str]
    __unique__: ClassVar[tuple[tuple[str, ...], ...]] = ()

    id: Id
    created_at: AwareDatetime


class TenantRow(Row):
    tenant_id: Id = fk("tenant")


class ModelMade(BaseModel):
    model_id: str | None = None
    prompt_version: str | None = None


# ====================================================================== operators, identities, tenancy
# operator and identity sit ABOVE tenants and are not tenant-scoped. The app role has no
# grants on them; login and the operator console reach them only through owner-run code.

class Operator(Row):
    """Whoever runs clients: Sai today, an agency later (operator edition, WP-41)."""

    __table__ = "operator"
    name: str


class Identity(Row):
    """A login. One person, one identity, any number of tenant memberships (app_user rows)."""

    __table__ = "identity"
    email: str = unique()
    display_name: str


class Tenant(Row):
    __table__ = "tenant"
    operator_id: Id = fk("operator")
    name: str
    slug: str = unique()
    default_language: Language
    region_code: str
    currency: str = Field(pattern=r"^[A-Z]{3}$", description="ISO 4217; every client-money column is in this")
    timezone: str = "UTC"
    paused: bool = False  # brand kill switch


class AppUser(TenantRow):
    """A membership: this identity's role inside this tenant. Approvals and audit point here."""

    __table__ = "app_user"
    __unique__ = (("tenant_id", "identity_id"),)
    identity_id: Id = fk("identity")
    display_name: str
    role: UserRole
    language: Language | None = None


class RegionPolicy(TenantRow):
    __table__ = "region_policy"
    __unique__ = (("tenant_id", "region_code", "version"),)
    region_code: str
    version: int
    rules: dict[str, Any] = Field(default_factory=dict, description="Filled from a lawyer's answers")
    disclosure_label: str | None = None
    effective_from: AwareDatetime


# ====================================================================== connections

class Connection(TenantRow):
    """One provider serving one capability for a scope. priority 0 is the default, then fallbacks."""

    __table__ = "connection"
    capability: Capability
    provider: str
    mode: ConnectionMode
    model: str | None = None
    role: TextRole | None = None
    scope: str = "tenant"  # tenant | brand | format | piece
    scope_ref: str | None = None
    priority: int = 0
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None


class ChannelAccount(TenantRow):
    __table__ = "channel_account"
    channel: Channel
    connection_id: Id | None = fk("connection", default=None)
    handle: str
    token_expires_at: AwareDatetime | None = None
    secret_ref: str | None = None


# ====================================================================== growth model and plays

class Play(TenantRow):
    __table__ = "play"
    __unique__ = (("tenant_id", "code"),)
    code: str
    name: str
    channels: list[Channel] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    qualification_questions: list[dict[str, Any]] = Field(default_factory=list)
    booked_definition: str
    pillar_mix: dict[str, float] = Field(default_factory=dict)
    metrics: list[str] = Field(default_factory=list)


class GrowthModel(TenantRow, ModelMade):
    __table__ = "growth_model"
    __unique__ = (("tenant_id", "version"),)
    version: int
    play_id: Id | None = fk("play", default=None)
    stages: list[dict[str, Any]] = Field(default_factory=list, description="Each stage: name, rate, low, high, tag")
    bottleneck: str | None = None
    margin_per_won: Money | None = None  # tenant currency
    share_of_margin: Decimal = Decimal("0.30")
    allowable_cost_eur: Money | None = None
    active: bool = False


class Pillar(TenantRow):
    __table__ = "pillar"
    growth_model_id: Id = fk("growth_model")
    name: str
    stage: FunnelStage
    target_share: Decimal
    min_share: Decimal = Decimal("0.10")


# ====================================================================== sources and truth

class Consent(TenantRow):
    __table__ = "consent"
    person_id: Id | None = fk("person", default=None)
    subject_label: str | None = None  # for staff or customers filmed but not in the lead tables
    basis: ConsentBasis
    channel: MessageChannel | None = None
    scope: str  # contact | likeness | voice | quote
    evidence: str
    given_at: AwareDatetime
    withdrawn_at: AwareDatetime | None = None


class Media(TenantRow, ModelMade):
    __table__ = "media"
    __unique__ = (("tenant_id", "sha256"),)
    kind: MediaKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: str
    size_bytes: int
    storage_uri: str
    provenance: MediaProvenance
    connection_id: Id | None = fk("connection", default=None)
    generation_prompt: str | None = None
    consent_id: Id | None = fk("consent", default=None)
    alt_text: str | None = None
    disclosure_label: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


class Source(TenantRow):
    __table__ = "source"
    kind: SourceKind
    language: Language | None = None
    title: str
    transcript: str | None = None
    uri: str | None = None
    media_id: Id | None = fk("media", default=None)
    consent_id: Id | None = fk("consent", default=None)
    untrusted: bool = False
    captured_at: AwareDatetime


class Claim(TenantRow, ModelMade):
    """Append-only by version: a change writes a new row (version + 1, supersedes_id set) and
    voids the old one, so the exact fact a published piece used stays on record."""

    __table__ = "claim"
    __unique__ = (("tenant_id", "code", "version"),)
    code: str = Field(pattern=r"^C-\d{4,}$")
    version: int = Field(default=1, ge=1)
    supersedes_id: Id | None = fk("claim", default=None)
    kind: ClaimKind
    state: st.ClaimState = st.ClaimState.CANDIDATE
    value: str
    unit: str | None = None
    as_of: date | None = None
    source_id: Id | None = fk("source", default=None)
    source_span: dict[str, Any] | None = Field(default=None, description="start, end, text")
    evidence_sha256: str | None = None
    verified_by: Id | None = fk("app_user", default=None)
    verified_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    languages: dict[str, str] = Field(default_factory=dict, description="Approved wording per language code")


# ====================================================================== planning

class Signal(TenantRow):
    __table__ = "signal"
    kind: str
    channel: Channel | None = None
    provider: str
    payload: dict[str, Any] = Field(default_factory=dict)
    observed_at: AwareDatetime


class Idea(TenantRow, ModelMade):
    __table__ = "idea"
    pillar_id: Id | None = fk("pillar", default=None)
    state: st.IdeaState = st.IdeaState.PROPOSED
    stage: FunnelStage
    title: str
    rationale: str
    source_ids: list[str] = Field(default_factory=list)
    is_experiment: bool = False


class IdeaSignal(TenantRow):
    __table__ = "idea_signal"
    __unique__ = (("idea_id", "signal_id"),)
    idea_id: Id = fk("idea")
    signal_id: Id = fk("signal")


class Brief(TenantRow, ModelMade):
    __table__ = "brief"
    idea_id: Id = fk("idea", unique=True)
    audience: str
    job_to_be_done: str
    message: str
    channels: list[Channel] = Field(default_factory=list)
    allowed_claim_ids: list[str] = Field(default_factory=list)
    call_to_action: str
    language: Language


class Forecast(TenantRow, ModelMade):
    """Sealed interval forecast plus a naive baseline, hash-chained per tenant."""

    __table__ = "forecast"
    __unique__ = (("tenant_id", "seq"),)
    brief_id: Id = fk("brief", unique=True)
    seq: int
    metric: str
    day: MetricDay
    low: float
    high: float
    baseline_low: float
    baseline_high: float
    sealed_at: AwareDatetime
    prev_hash: str | None = None
    seal_hash: str


class ForecastScore(TenantRow):
    __table__ = "forecast_score"
    forecast_id: Id = fk("forecast", unique=True)
    actual: float | None = None  # None = metric missing, never zero
    interval_score: float | None = None
    baseline_score: float | None = None
    beat_baseline: bool | None = None
    scored_at: AwareDatetime


# ====================================================================== production

class Asset(TenantRow, ModelMade):
    __table__ = "asset"
    brief_id: Id = fk("brief")
    master_text: str
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    selection_reason: str | None = None


class Bundle(TenantRow):
    __table__ = "bundle"
    asset_id: Id = fk("asset")
    state: st.BundleState = st.BundleState.OPEN
    root_hash: str | None = None
    rule_version: int


class Variant(TenantRow, ModelMade):
    """The asset is reached through the bundle, so the two can never disagree."""

    __table__ = "variant"
    bundle_id: Id = fk("bundle")
    channel: Channel
    language: Language
    state: st.VariantState = st.VariantState.DRAFTING
    version: int = 1
    text: str = Field(description="With claim slots like {{C-0012}}")
    rendered_text: str | None = None
    template_ref: str | None = None
    content_sha256: str | None = None
    scheduled_for: AwareDatetime | None = None
    task_type: str
    is_canary: bool = False
    canary_flaw: str | None = None


class VariantClaim(TenantRow):
    __table__ = "variant_claim"
    __unique__ = (("variant_id", "claim_id"),)
    variant_id: Id = fk("variant")
    claim_id: Id = fk("claim")


class VariantMedia(TenantRow):
    __table__ = "variant_media"
    __unique__ = (("variant_id", "media_id"),)
    variant_id: Id = fk("variant")
    media_id: Id = fk("media")
    position: int = 0


class CheckResult(TenantRow, ModelMade):
    __table__ = "check_result"
    variant_id: Id = fk("variant")
    layer: CheckLayer
    check_name: str
    verdict: CheckVerdict
    reason: str | None = None
    attempt: int = 1


class Approval(TenantRow):
    """Bound to an exact content hash. Any byte, claim or rule change voids it."""

    __table__ = "approval"
    subject_kind: ApprovalSubject  # bundle, spend_proposal, message_template, autonomy_state, rule_proposal
    subject_id: Id = index()
    content_hash: str = Field(description="Bundle root for bundles, hash of the signed object otherwise")
    approver_id: Id = fk("app_user")
    method: ApprovalMethod
    seconds_on_screen: float | None = None
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    canary: bool = False
    voided_at: AwareDatetime | None = None
    void_reason: str | None = None


class Publication(TenantRow):
    __table__ = "publication"
    __unique__ = (("tenant_id", "variant_id"), ("tenant_id", "idempotency_key"))  # one go-live per variant
    variant_id: Id = fk("variant")
    approval_id: Id = fk("approval")
    connection_id: Id | None = fk("connection", default=None)
    idempotency_key: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="The exact bytes sent")
    external_id: str | None = None
    live_url: str | None = None
    sent_at: AwareDatetime | None = None
    readback_at: AwareDatetime | None = None
    readback_ok: bool | None = None
    readback_diff: str | None = None


class MetricSnapshot(TenantRow):
    __table__ = "metric_snapshot"
    __unique__ = (("publication_id", "day"),)
    publication_id: Id = fk("publication")
    day: MetricDay
    metrics: dict[str, float] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list, description="Metrics the source did not return")
    pulled_at: AwareDatetime


class Edit(TenantRow):
    __table__ = "edit"
    variant_id: Id = fk("variant")
    editor_id: Id = fk("app_user")
    before_text: str
    after_text: str
    kind: EditKind | None = None
    reason: str | None = None
    rejection_reason: RejectionReason | None = None
    counts_as_defect: bool = False


class RuleProposal(TenantRow, ModelMade):
    __table__ = "rule_proposal"
    state: st.RuleProposalState = st.RuleProposalState.PROPOSED
    scope: RuleScope
    scope_ref: str | None = None
    rule: dict[str, Any]
    triggering_edit_ids: list[str] = Field(default_factory=list)
    regression_case: dict[str, Any] = Field(description="Triggering draft and the human final version")
    expires_at: AwareDatetime | None = None


class VoiceRule(TenantRow):
    __table__ = "voice_rule"
    rule_proposal_id: Id | None = fk("rule_proposal", default=None)
    scope: RuleScope
    scope_ref: str | None = None
    language: Language | None = None
    body: dict[str, Any]
    last_used_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None


# ====================================================================== tracking, people, leads

class Account(TenantRow):
    __table__ = "account"
    name: str
    domain: str | None = None


class Person(TenantRow):
    __table__ = "person"
    account_id: Id | None = fk("account", default=None)
    display_name: str | None = None
    phone: str | None = None
    email: str | None = None
    language: Language | None = None


class Campaign(TenantRow):
    """Budgets, the stopping rule and channel-role targets all work per campaign."""

    __table__ = "campaign"
    __unique__ = (("tenant_id", "platform", "external_id"),)
    platform: str
    external_id: str | None = None
    name: str
    role: CampaignRole
    state: st.AdState = st.AdState.CREATED_PAUSED
    daily_budget: Money | None = None  # tenant currency


class Ad(TenantRow):
    __table__ = "ad"
    campaign_id: Id = fk("campaign")
    variant_id: Id | None = fk("variant", default=None)
    platform: str
    external_id: str | None = None
    state: st.AdState = st.AdState.CREATED_PAUSED


class TrackedLink(TenantRow):
    __table__ = "tracked_link"
    variant_id: Id | None = fk("variant", default=None)
    ad_id: Id | None = fk("ad", default=None)
    slug: str = unique()
    destination: str
    whatsapp_code: str | None = unique(default=None)


class Touch(TenantRow):
    """No IP address, ever."""

    __table__ = "touch"
    kind: TouchKind
    person_id: Id | None = fk("person", default=None)
    variant_id: Id | None = fk("variant", default=None)
    ad_id: Id | None = fk("ad", default=None)
    tracked_link_id: Id | None = fk("tracked_link", default=None)
    code: str | None = None
    self_report: str | None = None
    occurred_at: AwareDatetime


class Lead(TenantRow):
    """Fit and heat are two numbers and are never blended."""

    __table__ = "lead"
    person_id: Id = fk("person")
    state: st.LeadState = st.LeadState.NEW
    route: LeadRoute | None = None
    fit: int = Field(ge=0, le=100, default=0)
    heat: float = 0.0
    heat_updated_at: AwareDatetime | None = None
    answers: dict[str, Any] = Field(default_factory=dict)
    first_touch_id: Id | None = fk("touch", default=None)
    last_touch_id: Id | None = fk("touch", default=None)
    owner_id: Id | None = fk("app_user", default=None)
    clock_started_at: AwareDatetime | None = None
    first_response_at: AwareDatetime | None = None


class MessageTemplate(TenantRow):
    """A once-approved message (acknowledgement, nurture, reminder). Slots fill only from
    verified data named in slot_sources. Approved by hash as ApprovalSubject.TEMPLATE."""

    __table__ = "message_template"
    __unique__ = (("tenant_id", "name", "version"),)
    name: str
    version: int
    kind: MessageKind
    channel: MessageChannel
    language: Language
    body: str
    slot_sources: dict[str, str] = Field(default_factory=dict, description="slot name to verified source")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active: bool = False


class Message(TenantRow, ModelMade):
    __table__ = "message"
    person_id: Id = fk("person")
    lead_id: Id | None = fk("lead", default=None)  # newsletter, proof and webinar messages have no lead
    kind: MessageKind
    channel: MessageChannel
    state: st.MessageState = st.MessageState.DRAFTED
    body: str
    template_id: Id | None = fk("message_template", default=None)
    template_approval_id: Id | None = fk("approval", default=None)
    sent_by_kind: ActorKind | None = None
    sent_by_user_id: Id | None = fk("app_user", default=None)
    sent_at: AwareDatetime | None = None


class Booking(TenantRow):
    __table__ = "booking"
    lead_id: Id = fk("lead")
    state: st.BookingState = st.BookingState.BOOKED
    kind: str  # demo, showroom visit, test drive, callback
    starts_at: AwareDatetime
    provider_ref: str | None = None


class Deal(TenantRow):
    __table__ = "deal"
    booking_id: Id | None = fk("booking", unique=True, default=None)  # None: walk-in or phone sale
    lead_id: Id = fk("lead")
    value: Money | None = None  # tenant currency


class Outcome(TenantRow):
    __table__ = "outcome"
    deal_id: Id = fk("deal", unique=True)
    kind: OutcomeKind
    amount: Money | None = None  # tenant currency
    loss_reason: LossReason | None = None
    recorded_by: Id = fk("app_user")
    occurred_at: AwareDatetime


# ====================================================================== money

class SpendSnapshot(TenantRow):
    __table__ = "spend_snapshot"
    __unique__ = (("tenant_id", "campaign_id", "ad_id", "day"),)
    campaign_id: Id = fk("campaign")
    ad_id: Id | None = fk("ad", default=None)  # None: a campaign-level row (search, CSV import)
    day: date
    spend: Money  # tenant currency
    impressions: int | None = None
    clicks: int | None = None
    pulled_at: AwareDatetime


class SpendProposal(TenantRow):
    __table__ = "spend_proposal"
    state: st.SpendProposalState = st.SpendProposalState.PROPOSED
    campaign_id: Id = fk("campaign")
    ad_id: Id | None = fk("ad", default=None)
    from_daily: Money
    to_daily: Money
    reason: str
    evidence_window: str
    rule_fired: str


# ====================================================================== trust, audit, operations

class AutonomyState(TenantRow):
    __table__ = "autonomy_state"
    __unique__ = (("tenant_id", "task_type"),)
    task_type: str
    level: AutonomyLevel = AutonomyLevel.L0_SHADOW
    window_items: int = 0
    defects: int = 0
    escaped: int = 0
    canaries_shown: int = 0
    canaries_caught: int = 0
    paused: bool = False
    changed_at: AwareDatetime


class PromptVersion(TenantRow):
    __table__ = "prompt_version"
    __unique__ = (("tenant_id", "name", "version"),)
    name: str
    version: int
    role: TextRole
    body_sha256: str
    model_id: str
    eval_passed: bool = False
    activated_at: AwareDatetime | None = None


class Incident(TenantRow):
    __table__ = "incident"
    severity: IncidentSeverity
    title: str
    subject_kind: str | None = None
    subject_id: str | None = None
    note: str | None = None
    opened_at: AwareDatetime
    closed_at: AwareDatetime | None = None


class CostEntry(TenantRow):
    __table__ = "cost_ledger"
    capability: Capability
    provider: str
    model: str | None = None
    units: Decimal = Decimal(0)
    cost_eur: Money
    run_ref: str
    subject_kind: str | None = None
    subject_id: str | None = None
    occurred_at: AwareDatetime


class AuditEntry(TenantRow):
    """Hash-chained per tenant. WP-04 owns the chain logic; this is the row shape."""

    __table__ = "audit_log"
    __unique__ = (("tenant_id", "seq"),)
    seq: int
    actor_kind: ActorKind
    actor_id: str
    action: str
    subject_kind: str | None = None
    subject_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str | None = None
    entry_hash: str
    occurred_at: AwareDatetime


class EventRecord(TenantRow):
    """Outbox for events. Automation hooks read from here, they never write back."""

    __table__ = "event_log"
    name: str = index()
    version: int = 1
    actor_kind: ActorKind
    actor_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: AwareDatetime


class EventDelivery(TenantRow):
    """One row per event per consumer (each hook, the dispatcher). event_log stays append-only."""

    __table__ = "event_delivery"
    __unique__ = (("event_id", "consumer"),)
    event_id: Id = fk("event_log")
    consumer: str
    attempts: int = 0
    delivered_at: AwareDatetime | None = None
    last_error: str | None = None


class PlanSlot(TenantRow):
    """A week-plan slot. Shared by the publisher (bench fills a miss), the review deck, the planner,
    measurement and trust, so it lives in core."""

    __table__ = "plan_slot"
    week_start: date
    channel: Channel
    pillar_id: Id | None = fk("pillar", default=None)
    stage: FunnelStage
    starts_at: AwareDatetime
    reserved_for_experiment: bool = False
    variant_id: Id | None = fk("variant", default=None)
    state: st.PlanSlotState = st.PlanSlotState.OPEN


class Job(TenantRow):
    """Postgres queue, claimed with FOR UPDATE SKIP LOCKED by one worker."""

    __table__ = "job"
    __unique__ = (("tenant_id", "idempotency_key"),)
    kind: str
    state: st.JobState = st.JobState.QUEUED
    payload: dict[str, Any] = Field(default_factory=dict)
    run_at: AwareDatetime = index()
    attempts: int = 0
    max_attempts: int = 5
    idempotency_key: str | None = None
    locked_by: str | None = None
    locked_at: AwareDatetime | None = None
    last_error: str | None = None


# Creation order respects foreign keys. Every table in BUILD_PLAN 2.3 is here, plus shared ones.
TABLES: tuple[type[Row], ...] = (
    Operator, Identity, Tenant, AppUser, RegionPolicy, Connection, ChannelAccount,
    Play, GrowthModel, Pillar,
    Account, Person, Consent, Media, Source, Claim,
    Signal, Idea, IdeaSignal, Brief, Forecast, ForecastScore,
    Asset, Bundle, Variant, VariantClaim, VariantMedia, CheckResult, PlanSlot,
    Approval, Publication, MetricSnapshot, Edit, RuleProposal, VoiceRule,
    Campaign, Ad, TrackedLink, Touch, Lead, MessageTemplate, Message, Booking, Deal, Outcome,
    SpendSnapshot, SpendProposal,
    AutonomyState, PromptVersion, Incident, CostEntry, AuditEntry, EventRecord, EventDelivery, Job,
)

# Entities named in the 2.3 diagram, mapped to their table. Tested for completeness.
ER_ENTITIES: dict[str, str] = {
    "TENANT": "tenant", "GROWTH_MODEL": "growth_model", "CLAIM": "claim", "SOURCE": "source",
    "PILLAR": "pillar", "SIGNAL": "signal", "IDEA": "idea", "BRIEF": "brief", "FORECAST": "forecast",
    "ASSET": "asset", "VARIANT": "variant", "MEDIA": "media", "CHECK_RESULT": "check_result",
    "BUNDLE": "bundle", "APPROVAL": "approval", "PUBLICATION": "publication",
    "METRIC_SNAPSHOT": "metric_snapshot", "TRACKED_LINK": "tracked_link", "TOUCH": "touch", "AD": "ad",
    "SPEND_SNAPSHOT": "spend_snapshot", "PERSON": "person", "CONSENT": "consent", "ACCOUNT": "account",
    "LEAD": "lead", "MESSAGE": "message", "BOOKING": "booking", "DEAL": "deal", "OUTCOME": "outcome",
    "FORECAST_SCORE": "forecast_score", "EDIT": "edit", "RULE_PROPOSAL": "rule_proposal",
    "VOICE_RULE": "voice_rule", "SPEND_PROPOSAL": "spend_proposal",
}

