"""The connection contract (BUILD_PLAN 2.1b).

The core never calls a vendor. It calls a capability, and the registry (WP-02) picks the
connection. Every provider, manual ones included, implements the same five calls:

    describe()        what it can do, limits, models, unit cost, licence note
    health()          connected, token expiry, quota left
    estimate(request) cost and time before anything runs
    run(request)      result with provenance, cost and external ids
    mock()            a deterministic fake for tests and the demo client

Safety lives in the request types, so no provider can route around the gate:
- Publishing requests must carry an approval id and the content hash it approved.
- Message sends must carry a template approval or the id of the human sending it.
- Ads can only be created paused; spend changes must carry a signed proposal's approval.
- Untrusted text (leads, comments, research) travels as quoted data, never as instructions.
- Automation hooks receive events. They have no way to ask for anything.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from thallo.contracts.actors import HumanActor
from thallo.contracts.enums import (
    Capability,
    Channel,
    ConnectionMode,
    Language,
    MediaKind,
    MediaProvenance,
    MessageChannel,
    TextRole,
)
from thallo.contracts.events import Event
from thallo.contracts.ids import Id

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ====================================================================== the shared shapes

class ProviderInfo(_Frozen):
    capability: Capability
    name: str
    mode: ConnectionMode
    models: tuple[str, ...] = ()
    limits: dict[str, Any] = Field(default_factory=dict)
    unit_cost_eur: dict[str, Decimal] = Field(default_factory=dict, description="Per unit, e.g. per_1m_input_tokens")
    licence_note: str | None = None
    sends_externally: bool = False  # true for anything that reaches a third party's audience


class Health(_Frozen):
    connected: bool
    checked_at: AwareDatetime
    token_expires_at: AwareDatetime | None = None
    quota_left: Decimal | None = None
    detail: str | None = None


class Estimate(_Frozen):
    cost_eur: Decimal
    seconds: float


class Provenance(_Frozen):
    made_by: Literal["person", "provider"]
    provider: str
    mode: ConnectionMode
    model: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    made_at: AwareDatetime


class Request(_Frozen):
    capability: ClassVar[Capability]
    tenant_id: str
    idempotency_key: str | None = None
    cost_cap_eur: Decimal | None = None


class Result(_Frozen):
    pass


class RunResult(_Frozen):
    """status "pending_person": a manual connection prepared a task (copy pack, checklist,
    click-to-chat link) and a person must act; `task` says what. The output is a placeholder until
    the person confirms, and nothing downstream may treat it as done."""

    output: Result
    status: Literal["done", "pending_person"] = "done"
    task: str | None = None
    provenance: Provenance
    cost_eur: Decimal
    seconds: float = 0.0
    external_ids: dict[str, str] = Field(default_factory=dict)


class MediaResult(Result):
    kind: MediaKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: str
    storage_uri: str
    provenance: MediaProvenance
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    disclosure_required: bool = False


# ====================================================================== text

class TextRequest(Request):
    capability: ClassVar[Capability] = Capability.TEXT
    role: TextRole
    instructions: str
    untrusted_data: tuple[str, ...] = Field(default=(), description="Quoted data, never placed as instructions")
    output_schema: dict[str, Any] | None = None
    language: Language | None = None
    max_output_tokens: int = 2000


class TextResult(Result):
    text: str
    data: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0


# ====================================================================== media

class ImageRequest(Request):
    capability: ClassVar[Capability] = Capability.IMAGE
    purpose: Literal["background", "illustration", "template_render", "upload"]
    prompt: str | None = None
    template_ref: str | None = None
    slots: dict[str, str] = Field(default_factory=dict)
    width: int
    height: int
    allow_generated_people: bool = False
    consent_id: str | None = None

    @model_validator(mode="after")
    def _people_need_consent(self) -> ImageRequest:
        if self.allow_generated_people and not self.consent_id:
            raise ValueError("generated people need a consent record")
        return self


class VideoRequest(Request):
    capability: ClassVar[Capability] = Capability.VIDEO
    action: Literal["cut", "caption", "template_render", "generate", "upload"]
    input_sha256: tuple[str, ...] = ()
    script: str | None = None
    template_ref: str | None = None
    width: int
    height: int
    duration_ms: int | None = None
    allow_generated_people: bool = False
    consent_id: str | None = None

    @model_validator(mode="after")
    def _people_need_consent(self) -> VideoRequest:
        if self.allow_generated_people and not self.consent_id:
            raise ValueError("generated people need a consent record")
        return self


class TranscriptionRequest(Request):
    capability: ClassVar[Capability] = Capability.TRANSCRIPTION
    media_sha256: str
    language_hint: Language | None = None
    typed_text: str | None = None  # manual mode


class TranscriptionResult(Result):
    text: str
    language: Language | None = None
    segments: tuple[dict[str, Any], ...] = ()


class VoiceoverRequest(Request):
    capability: ClassVar[Capability] = Capability.VOICEOVER
    text: str
    language: Language
    voice: str
    is_real_person_voice: bool = False
    consent_id: str | None = None

    @model_validator(mode="after")
    def _real_voice_needs_consent(self) -> VoiceoverRequest:
        if self.is_real_person_voice and not self.consent_id:
            raise ValueError("a real person's voice needs a consent record")
        return self


# ====================================================================== publishing

class PublishRequest(Request):
    capability: ClassVar[Capability] = Capability.PUBLISHING
    idempotency_key: str = Field(min_length=8)  # required here
    channel: Channel
    channel_account_id: Id
    variant_id: Id
    approval_id: Id
    content_hash: Sha256
    text: str
    media_sha256: tuple[str, ...] = ()
    scheduled_for: AwareDatetime | None = None


class PublishResult(Result):
    status: Literal["scheduled", "published", "manual_pending"]
    external_id: str | None = None
    live_url: str | None = None
    copy_pack: dict[str, Any] | None = None  # manual mode: what the person posts


class ReadbackRequest(Request):
    capability: ClassVar[Capability] = Capability.PUBLISHING
    publication_id: str
    external_id: str | None = None
    live_url: str | None = None


class ReadbackResult(Result):
    live_text: str | None
    media_changed: bool | None = None


# ====================================================================== calendar, crm, booking

class CalendarRequest(Request):
    capability: ClassVar[Capability] = Capability.CALENDAR
    slots: tuple[dict[str, Any], ...]  # one-way mirror out


class CalendarResult(Result):
    mirrored: int
    comments_in: tuple[dict[str, Any], ...] = ()


class CrmSyncRequest(Request):
    capability: ClassVar[Capability] = Capability.CRM
    records: tuple[dict[str, Any], ...]  # one-way push


class CrmSyncResult(Result):
    synced: int
    external_ids: dict[str, str] = Field(default_factory=dict)


class BookingRequest(Request):
    capability: ClassVar[Capability] = Capability.BOOKING
    action: Literal["link", "slots"]
    lead_id: str
    kind: str


class BookingResult(Result):
    link: str | None = None
    slots: tuple[AwareDatetime, ...] = ()


# ====================================================================== messaging

class MessageSendRequest(Request):
    capability: ClassVar[Capability] = Capability.MESSAGING
    message_id: Id
    channel: MessageChannel
    to: str = Field(min_length=1)
    body: str
    template_approval_id: Id | None = None
    consent_id: Id | None = None
    # The sending human. The messaging service calls require_human(FIRST_REPLY, verifier=...)
    # before building this request; providers must not forward the session anywhere.
    human: HumanActor | None = None

    @model_validator(mode="after")
    def _authorised(self) -> MessageSendRequest:
        if bool(self.template_approval_id) == bool(self.human):
            raise ValueError("a send needs exactly one of: a template approval, or the human sending it")
        if self.template_approval_id and not self.consent_id:
            raise ValueError("a template instance needs the recipient's consent record")
        if self.human and self.human.tenant_id != self.tenant_id:
            raise ValueError("sender belongs to another tenant")
        return self


class MessageSendResult(Result):
    status: Literal["sent", "queued", "manual_link"]
    external_id: str | None = None
    click_to_chat_url: str | None = None  # manual mode opens the seller's own app


# ====================================================================== analytics, ads, research

class AnalyticsRequest(Request):
    capability: ClassVar[Capability] = Capability.ANALYTICS
    source: str
    metrics: tuple[str, ...]
    start: date
    end: date
    refs: tuple[str, ...] = ()


class AnalyticsResult(Result):
    rows: tuple[dict[str, Any], ...]
    missing: tuple[str, ...] = ()  # missing is missing, never zero
    raw_ref: str | None = None


class AdsAction(StrEnum):
    CREATE_PAUSED = "create_paused"
    PAUSE = "pause"
    APPLY_SIGNED = "apply_signed"
    PULL_SPEND = "pull_spend"
    UPLOAD_OUTCOMES = "upload_outcomes"


# Any key, at any depth, whose name contains one of these is refused in a free-form payload.
_MONEY_WORDS = ("status", "state", "budget", "bid", "spend", "amount", "micros", "cost", "price", "pacing",
                "active", "enable", "schedule", "objective_value")


def _money_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if any(w in str(k).lower() for w in _MONEY_WORDS):
                found.append(here)
            found += _money_keys(v, here)
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            found += _money_keys(v, f"{path}[{i}]")
    return found


class AdsRequest(Request):
    """Money moves only through APPLY_SIGNED, carrying the exact signed numbers.

    The provider applies `to_daily` (in `currency`) to `campaign_external_id` and nothing else. Free-form
    `payload` may never carry a status, budget, bid, spend or schedule key at any depth, and every
    provider must itself force the paused status on create rather than trust the payload.
    """

    capability: ClassVar[Capability] = Capability.ADS
    action: AdsAction
    platform: str = Field(min_length=1)
    campaign_external_id: str | None = None
    ad_id: Id | None = None
    spend_proposal_id: Id | None = None
    approval_id: Id | None = None
    approved_content_hash: Sha256 | None = None
    to_daily: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")  # the tenant's currency
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _money_needs_signature(self) -> AdsRequest:
        bad = _money_keys(self.payload)
        if bad:
            raise ValueError(f"payload may not set {bad}; spend moves only via APPLY_SIGNED")
        if self.action is AdsAction.APPLY_SIGNED:
            if not (self.spend_proposal_id and self.approval_id and self.approved_content_hash
                    and self.to_daily is not None and self.currency and self.campaign_external_id):
                raise ValueError("a spend change needs the signed proposal, its approval and hash, "
                                 "the campaign and the signed daily amount")
        elif self.to_daily is not None:
            raise ValueError("to_daily is only allowed with APPLY_SIGNED")
        return self


class AdsResult(Result):
    status: str
    rows: tuple[dict[str, Any], ...] = ()


class ResearchRequest(Request):
    capability: ClassVar[Capability] = Capability.RESEARCH
    query: str
    published_before: date | None = None


class ResearchResult(Result):
    items: tuple[dict[str, Any], ...]  # url, fetched_at, text; always untrusted
    untrusted: Literal[True] = True


# ====================================================================== storage, notifications, hooks

class StorageRequest(Request):
    capability: ClassVar[Capability] = Capability.STORAGE
    action: Literal["put", "get"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: bytes | None = None
    mime: str | None = None


class StorageResult(Result):
    uri: str
    sha256: str
    content: bytes | None = None


class NotifyRequest(Request):
    capability: ClassVar[Capability] = Capability.NOTIFICATIONS
    user_id: str  # internal users only, never a lead
    title: str
    body: str
    link: str | None = None


class NotifyResult(Result):
    delivered: bool


class HookRequest(Request):
    capability: ClassVar[Capability] = Capability.AUTOMATION_HOOKS
    event: Event

    @model_validator(mode="after")
    def _same_tenant(self) -> HookRequest:
        if self.event.tenant_id != self.tenant_id:
            raise ValueError("a hook may only receive its own tenant's events")
        return self


class HookResult(Result):
    delivered: bool


REQUEST_TYPES: dict[Capability, tuple[type[Request], ...]] = {
    Capability.TEXT: (TextRequest,),
    Capability.IMAGE: (ImageRequest,),
    Capability.VIDEO: (VideoRequest,),
    Capability.TRANSCRIPTION: (TranscriptionRequest,),
    Capability.VOICEOVER: (VoiceoverRequest,),
    Capability.PUBLISHING: (PublishRequest, ReadbackRequest),
    Capability.CALENDAR: (CalendarRequest,),
    Capability.CRM: (CrmSyncRequest,),
    Capability.MESSAGING: (MessageSendRequest,),
    Capability.BOOKING: (BookingRequest,),
    Capability.ANALYTICS: (AnalyticsRequest,),
    Capability.ADS: (AdsRequest,),
    Capability.RESEARCH: (ResearchRequest,),
    Capability.STORAGE: (StorageRequest,),
    Capability.NOTIFICATIONS: (NotifyRequest,),
    Capability.AUTOMATION_HOOKS: (HookRequest,),
}


class CapabilityMismatch(TypeError):
    pass


class CostCapExceeded(RuntimeError):
    """run() must refuse before spending when estimate() is above request.cost_cap_eur."""


@runtime_checkable
class Provider(Protocol):
    capability: Capability

    def describe(self) -> ProviderInfo: ...
    def health(self) -> Health: ...
    def estimate(self, request: Request) -> Estimate: ...
    def run(self, request: Request) -> RunResult: ...
    def mock(self) -> Provider: ...


def check_request(provider: Provider, request: Request) -> None:
    """Every provider calls this first in estimate() and run()."""
    if request.capability is not provider.capability or type(request) not in REQUEST_TYPES[provider.capability]:
        raise CapabilityMismatch(f"{provider.capability.value} provider got {type(request).__name__}")
    # Re-validate: a request built with model_construct() skips its validators.
    type(request).model_validate(request.model_dump())


def check_cap(estimate: Estimate, request: Request) -> None:
    if request.cost_cap_eur is not None and estimate.cost_eur > request.cost_cap_eur:
        raise CostCapExceeded(f"estimate {estimate.cost_eur} EUR is over the cap {request.cost_cap_eur} EUR")


# ====================================================================== selection

class OverrideScope(StrEnum):
    TENANT = "tenant"
    BRAND = "brand"
    FORMAT = "format"
    PIECE = "piece"


class ConnectionChoice(_Frozen):
    """Resolved by the registry: the default first, then fallbacks in order. Manual is always last."""

    capability: Capability
    role: TextRole | None = None
    ordered_connection_ids: tuple[str, ...]
    scope: OverrideScope = OverrideScope.TENANT
    scope_ref: str | None = None
