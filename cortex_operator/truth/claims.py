"""Claim lifecycle: candidate, human verification, expiry and change.

Pure functions over the contract `Claim` row. Each returns a new row plus the event to write in
the same transaction; nothing here stores anything.

- Only a verified live human session can verify a claim (`require_human`, VERIFY_CLAIM) or change
  one (CHANGE_CLAIM, contracts 2.4.0).
- Verify and change act on the latest version of a code only; both take the repository to check.
- A unitless NUMBER claim needs approved wording (`claim.languages`) in every tenant language, so
  the writer can never choose what the number counts (ten showrooms, years or customers).
- Prices and offers (ClaimKind.PRICE and OFFER) expire 30 days after verification by default. A
  human may set a date up to 90 days out; past that the date needs an `expiry_reason` of at least
  15 characters, and 365 days is a hard ceiling. Re-verification never silently extends an
  earlier, shorter expiry.
- A PRICE value may not carry more decimals than its currency has minor units (JPY none, KWD three).
- A change to a claim's value, unit, date, evidence or approved wording writes a new version
  (append-only, contracts 2.0.0), voids the old row (verified or expired) and lists every approval
  it voids.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from cortex_operator.contracts import sha256
from cortex_operator.contracts.actors import Actor, SessionVerifier, SystemActor, require_human
from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.enums import ClaimKind, HumanOnlyAction, Language
from cortex_operator.contracts.events import ClaimChangedPayload, ClaimPayload, Event, EventName
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import Claim
from cortex_operator.contracts.states import CLAIM, ClaimState
from cortex_operator.truth.repository import ClaimRepository
from cortex_operator.truth.units import is_currency, is_number_unit, minor_units

NUMERIC = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")  # no leading zeros: refuses "007"
DATE_VALUE = re.compile(r"^[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?$")
CODE = re.compile(r"^C-([0-9]{4,})$")
SYSTEM = SystemActor(id="truth")


class ClaimError(ValueError):
    pass


@dataclass(frozen=True)
class ExpiryPolicy:
    """Default lifetime per kind, the longest a human may set without a reason, and the ceiling.
    Prices and offers default to 30 days and may be set to at most 90; a longer date needs an
    `expiry_reason` of at least `min_reason` characters, stored on the claim (contracts 2.1.0),
    and no date may lie past the ceiling (365 days)."""

    ttl_by_kind: Mapping[ClaimKind, timedelta] = field(
        default_factory=lambda: {ClaimKind.PRICE: timedelta(days=30), ClaimKind.OFFER: timedelta(days=30)})
    max_by_kind: Mapping[ClaimKind, timedelta] = field(
        default_factory=lambda: {ClaimKind.PRICE: timedelta(days=90), ClaimKind.OFFER: timedelta(days=90)})
    ceiling_by_kind: Mapping[ClaimKind, timedelta] = field(
        default_factory=lambda: {ClaimKind.PRICE: timedelta(days=365), ClaimKind.OFFER: timedelta(days=365)})
    min_reason: int = 15

    def ttl(self, kind: ClaimKind) -> timedelta | None:
        return self.ttl_by_kind.get(kind)

    def max(self, kind: ClaimKind) -> timedelta | None:
        return self.max_by_kind.get(kind)

    def ceiling(self, kind: ClaimKind) -> timedelta | None:
        return self.ceiling_by_kind.get(kind)


DEFAULT_POLICY = ExpiryPolicy()


# ---------------------------------------------------------------- validation and codes

def check_value(kind: ClaimKind, value: str, unit: str | None) -> None:
    """Values are stored in a canonical, language-free form so code can render them per language.
    NUMBER and PRICE: ASCII digits, no leading zeros or negative zero, optional dot decimal
    ("3.5"). NUMBER may carry a unit from `units.py`; PRICE needs an ISO 4217 code and at most
    that currency's minor units of decimals; no other kind takes a unit. DATE: "YYYY", "YYYY-MM"
    or "YYYY-MM-DD" in ASCII digits."""
    if not value or value != value.strip():
        raise ClaimError("claim value is empty or padded")
    if kind in (ClaimKind.NUMBER, ClaimKind.PRICE):
        if not NUMERIC.match(value):
            raise ClaimError(f"{kind.value} value must be canonical digits like 3.5, got {value!r}")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover, the regex already guards this
            raise ClaimError(str(exc)) from exc
        if number == 0 and value.startswith("-"):
            raise ClaimError(f"negative zero is not a canonical value: {value!r}")
    if kind is ClaimKind.NUMBER:
        if unit is not None and not is_number_unit(unit):
            raise ClaimError(f"unknown unit {unit!r}; add it to truth/units.py first")
    elif kind is ClaimKind.PRICE:
        if unit is None or not is_currency(unit):
            raise ClaimError(f"a price needs an ISO 4217 currency code such as EUR or INR, got {unit!r}")
        decimals = len(value.partition(".")[2])
        if decimals > minor_units(unit):
            raise ClaimError(f"{unit} has {minor_units(unit)} decimal places; {value!r} has {decimals} decimal places")
    elif unit is not None:
        raise ClaimError(f"a {kind.value} claim takes no unit")
    if kind is ClaimKind.DATE:
        if not DATE_VALUE.match(value):
            raise ClaimError(f"date value must be YYYY, YYYY-MM or YYYY-MM-DD, got {value!r}")
        parse_date_value(value)


def parse_date_value(value: str) -> tuple[int, int | None, int | None]:
    parts = value.split("-")
    try:
        if len(parts) == 1 and len(parts[0]) == 4:
            return int(parts[0]), None, None
        if len(parts) == 2:
            date(int(parts[0]), int(parts[1]), 1)
            return int(parts[0]), int(parts[1]), None
        if len(parts) == 3:
            d = date.fromisoformat(value)
            return d.year, d.month, d.day
    except ValueError as exc:
        raise ClaimError(f"date value must be YYYY, YYYY-MM or YYYY-MM-DD, got {value!r}") from exc
    raise ClaimError(f"date value must be YYYY, YYYY-MM or YYYY-MM-DD, got {value!r}")


def next_code(existing: Iterable[str]) -> str:
    """The next free claim code after the highest existing one: C-0001, C-0002, ..."""
    top = 0
    for code in existing:
        m = CODE.match(code)
        if m:
            top = max(top, int(m.group(1)))
    return f"C-{top + 1:04d}"


def problem(claim: Claim | None, now: datetime) -> str | None:
    """Why a claim may not be used in copy right now, or None if it may."""
    if claim is None:
        return "unknown claim"
    if claim.state is not ClaimState.VERIFIED:
        return f"claim {claim.code} is {claim.state.value}, not verified"
    if claim.verified_by is None or claim.verified_at is None:
        return f"claim {claim.code} has no named verifier"
    if claim.expires_at is not None and now >= claim.expires_at:
        return f"claim {claim.code} expired at {claim.expires_at.isoformat()}"
    return None


def check_latest(claim: Claim, repo: ClaimRepository) -> None:
    """Refuse a row that is not the latest version of its code in `repo`. A row not saved yet (a
    fresh candidate) is fine when no later version exists."""
    latest = repo.latest(claim.tenant_id, claim.code)
    if latest is None:
        return
    if latest.version > claim.version or (latest.version == claim.version and latest.id != claim.id):
        raise ClaimError(f"{claim.code} v{claim.version} is not the latest version (v{latest.version}); "
                         "act on the latest version")


def check_bound(claim: Claim, tenant_languages: Collection[Language] | None) -> None:
    """S4: a unitless NUMBER must say what it counts, in approved wording for every tenant language."""
    if claim.kind is not ClaimKind.NUMBER or claim.unit is not None:
        return
    if not tenant_languages:
        raise ClaimError(f"{claim.code}: a NUMBER without a unit needs approved wording; pass tenant_languages")
    missing = [lang.value for lang in tenant_languages if not (claim.languages.get(lang.value) or "").strip()]
    if missing:
        raise ClaimError(f"{claim.code}: a NUMBER without a unit needs approved wording in {', '.join(missing)}, "
                         "so the writer cannot choose what it counts")


# ---------------------------------------------------------------- helpers

def _rebuild(claim: Claim, **changes: Any) -> Claim:
    return Claim.model_validate({**claim.model_dump(), **changes})


def _event(clock: Clock, claim: Claim, actor: Actor, name: EventName,
           payload: ClaimPayload) -> Event:
    return Event.build(id=new_id(clock), tenant_id=claim.tenant_id, occurred_at=clock.now(), actor=actor,
                       payload=payload, name=name)


def _evidence_hash(evidence: str | bytes | None, evidence_sha256: str | None) -> str | None:
    if evidence is not None and evidence_sha256 is not None:
        raise ClaimError("pass evidence or evidence_sha256, not both")
    if evidence is None:
        return evidence_sha256
    if isinstance(evidence, bytes):
        return hashlib.sha256(evidence).hexdigest()
    return sha256(evidence)


# ---------------------------------------------------------------- lifecycle

def new_candidate(*, tenant_id: str, code: str, kind: ClaimKind, value: str, clock: Clock, actor: Actor,
                  unit: str | None = None, as_of: date | None = None, source_id: str | None = None,
                  source_span: dict[str, Any] | None = None, evidence: str | bytes | None = None,
                  evidence_sha256: str | None = None, languages: Mapping[str, str] | None = None,
                  expires_at: datetime | None = None, model_id: str | None = None,
                  prompt_version: str | None = None) -> tuple[Claim, Event]:
    """A claim candidate from extraction or typed by the operator. Not usable until verified."""
    check_value(kind, value, unit)
    now = clock.now()
    claim = Claim(
        id=new_id(clock), created_at=now, tenant_id=tenant_id, code=code, kind=kind,
        state=CLAIM.initial, value=value, unit=unit, as_of=as_of, source_id=source_id,
        source_span=source_span, evidence_sha256=_evidence_hash(evidence, evidence_sha256),
        expires_at=expires_at, languages=dict(languages or {}), model_id=model_id,
        prompt_version=prompt_version,
    )
    return claim, _event(clock, claim, actor, EventName.CLAIM_CANDIDATE_ADDED,
                         ClaimPayload(claim_id=claim.id, code=claim.code))


def verify_claim(claim: Claim, actor: Actor, *, verifier: SessionVerifier, clock: Clock, repo: ClaimRepository,
                 expires_at: datetime | None = None, expiry_reason: str | None = None,
                 tenant_languages: Collection[Language] | None = None,
                 policy: ExpiryPolicy = DEFAULT_POLICY) -> tuple[Claim, Event]:
    """Human-only. Moves a candidate (or an expired claim) to verified and sets its expiry.

    Only the latest version of a code may be verified (checked against `repo`). A unitless NUMBER
    needs approved wording for every language in `tenant_languages`.

    Expiry is, in order: the date the human gives; else the earlier of the claim's own future
    expiry and the policy default (30 days for prices and offers); else none. A claim whose own
    expiry has passed needs a new date from the human unless the policy supplies one."""
    human = require_human(actor, HumanOnlyAction.VERIFY_CLAIM, verifier=verifier, tenant_id=claim.tenant_id)
    CLAIM.check(claim.state, ClaimState.VERIFIED)
    check_latest(claim, repo)
    if claim.evidence_sha256 is None and claim.source_id is None:
        raise ClaimError(f"{claim.code}: no source and no evidence; nothing to verify against")
    check_value(claim.kind, claim.value, claim.unit)
    check_bound(claim, tenant_languages)
    now = clock.now()
    longest = policy.max(claim.kind)
    ceiling = policy.ceiling(claim.kind)
    reason = (expiry_reason or "").strip()
    if expires_at is not None:
        if expires_at <= now:
            raise ClaimError(f"{claim.code}: expiry must be in the future")
        if ceiling is not None and expires_at > now + ceiling:
            raise ClaimError(f"{claim.code}: a {claim.kind.value} may be verified for at most {ceiling.days} days")
        if longest is not None and expires_at > now + longest and len(reason) < policy.min_reason:
            raise ClaimError(f"{claim.code}: a {claim.kind.value} may be verified for at most {longest.days} days"
                             f" unless an expiry_reason of at least {policy.min_reason} characters is given")
        expiry: datetime | None = expires_at
    else:
        options = []
        ttl = policy.ttl(claim.kind)
        if ttl is not None:
            options.append(now + ttl)
        if claim.expires_at is not None:
            if claim.expires_at <= now and ttl is None:
                raise ClaimError(f"{claim.code}: its expiry has passed; give a new expiry date")
            if claim.expires_at > now:
                options.append(claim.expires_at)
        expiry = min(options) if options else None
        if ceiling is not None and expiry is not None and expiry > now + ceiling:
            expiry = now + ceiling
    long_dated = expires_at is not None and longest is not None and expires_at > now + longest
    verified = _rebuild(claim, state=ClaimState.VERIFIED, verified_by=human.id, verified_at=now, expires_at=expiry,
                        expiry_reason=reason if long_dated else None)
    return verified, _event(clock, verified, human, EventName.CLAIM_VERIFIED,
                            ClaimPayload(claim_id=verified.id, code=verified.code))


def reject_claim(claim: Claim, actor: Actor, *, clock: Clock) -> tuple[Claim, Event]:
    """A candidate (or an expired claim) the operator throws out. Terminal."""
    CLAIM.check(claim.state, ClaimState.REJECTED)
    rejected = _rebuild(claim, state=ClaimState.REJECTED)
    return rejected, _event(clock, rejected, actor, EventName.CLAIM_REJECTED,
                            ClaimPayload(claim_id=rejected.id, code=rejected.code))


def expire_due(claims: Iterable[Claim], clock: Clock, actor: Actor = SYSTEM) -> list[tuple[Claim, Event]]:
    """Every verified claim whose expiry has arrived, moved to expired, with its event."""
    now = clock.now()
    out = []
    for claim in claims:
        if claim.state is ClaimState.VERIFIED and claim.expires_at is not None and now >= claim.expires_at:
            CLAIM.check(claim.state, ClaimState.EXPIRED)
            expired = _rebuild(claim, state=ClaimState.EXPIRED)
            out.append((expired, _event(clock, expired, actor, EventName.CLAIM_EXPIRED,
                                        ClaimPayload(claim_id=expired.id, code=expired.code))))
    return out


MATERIAL = ("value", "unit", "as_of", "source_id", "source_span", "evidence_sha256", "languages")
_UNSET: Any = object()


@dataclass(frozen=True)
class ClaimChange:
    """`previous` is the old row, now VOIDED (it was verified or expired); `current` is the new
    version, a candidate awaiting a human. For a change to a candidate, which was never usable,
    `previous` is None and `current` is the edited row."""

    previous: Claim | None
    current: Claim
    events: tuple[Event, ...]  # CLAIM_CHANGED for the old row, then CLAIM_CANDIDATE_ADDED for the new one
    changed: tuple[str, ...]
    voided_variant_ids: tuple[str, ...]
    voided_approval_ids: tuple[str, ...]


def change_claim(claim: Claim, *, actor: Actor, verifier: SessionVerifier, clock: Clock, repo: ClaimRepository,
                 value: str = _UNSET, unit: str | None = _UNSET, as_of: date | None = _UNSET,
                 source_id: str | None = _UNSET, source_span: dict[str, Any] | None = _UNSET,
                 evidence: str | bytes | None = None, evidence_sha256: str | None = _UNSET,
                 languages: Mapping[str, str] | None = _UNSET) -> ClaimChange:
    """Human-only (CHANGE_CLAIM, contracts 2.4.0), because a change voids approvals. Changes what a
    claim says or what backs it. Claims are append-only by version (contracts 2.0.0), so the exact
    fact a published piece used is never overwritten:

    - VERIFIED or EXPIRED: the row goes to VOIDED (terminal) and a new row, version + 1 with
      `supersedes_id`, starts as a CANDIDATE until a human verifies it. Every variant and live
      approval that used the old row is listed; the caller voids those approvals (WP-04) and
      writes the events.
    - CANDIDATE: never usable in copy, so it is edited in place and nothing is voided.
    - REJECTED or VOIDED, or any row that is not the latest version in `repo`: refused."""
    require_human(actor, HumanOnlyAction.CHANGE_CLAIM, verifier=verifier, tenant_id=claim.tenant_id)
    if claim.state in (ClaimState.REJECTED, ClaimState.VOIDED):
        raise ClaimError(f"{claim.code} v{claim.version} is {claim.state.value}; change the latest version")
    check_latest(claim, repo)
    updates: dict[str, Any] = {}
    for name, new in (("value", value), ("unit", unit), ("as_of", as_of), ("source_id", source_id),
                      ("source_span", source_span), ("languages", languages)):
        if new is not _UNSET:
            updates[name] = dict(new) if name == "languages" and new is not None else new
    if evidence is not None or evidence_sha256 is not _UNSET:
        updates["evidence_sha256"] = _evidence_hash(evidence, None if evidence_sha256 is _UNSET else evidence_sha256)
    if updates.get("languages", ...) is None:
        updates["languages"] = {}
    changed = tuple(k for k in MATERIAL if k in updates and updates[k] != getattr(claim, k))
    if not changed:
        raise ClaimError(f"{claim.code}: nothing material changed")
    check_value(claim.kind, updates.get("value", claim.value), updates.get("unit", claim.unit))
    material = {k: updates[k] for k in changed}

    if claim.state is ClaimState.CANDIDATE:
        edited = _rebuild(claim, **material)
        return ClaimChange(previous=None, current=edited, events=(), changed=changed, voided_variant_ids=(),
                           voided_approval_ids=())

    now = clock.now()
    CLAIM.check(claim.state, ClaimState.VOIDED)
    previous = _rebuild(claim, state=ClaimState.VOIDED)
    keep_expiry = claim.expires_at if claim.expires_at is not None and claim.expires_at > now else None
    current = _rebuild(claim, **material, id=new_id(clock), created_at=now, version=claim.version + 1,
                       supersedes_id=claim.id, state=CLAIM.initial, verified_by=None, verified_at=None,
                       expires_at=keep_expiry, expiry_reason=claim.expiry_reason if keep_expiry else None)
    variants = tuple(repo.variants_using_claim(claim.tenant_id, claim.id))
    approvals = tuple(repo.live_approvals_for_variants(claim.tenant_id, list(variants))) if variants else ()
    events = (
        _event(clock, previous, actor, EventName.CLAIM_CHANGED,
               ClaimChangedPayload(claim_id=previous.id, code=previous.code, voided_approval_ids=list(approvals),
                                   voided_variant_ids=list(variants))),
        _event(clock, current, actor, EventName.CLAIM_CANDIDATE_ADDED,
               ClaimPayload(claim_id=current.id, code=current.code)),
    )
    return ClaimChange(previous=previous, current=current, events=events, changed=changed,
                       voided_variant_ids=variants, voided_approval_ids=approvals)
