"""The publish-time decision (WP-04). The last check before any variant goes out.

`decide` is pure: given a content snapshot (media bytes already hashed), the approval with its
evidence (the approver's user row and the audited grant), the tenant, the global pause and the
time, it returns allow or block with every reason found. `check_publish` loads all of that
through a `GateRepository` and calls `decide`. It never raises: any repository or hashing
failure is a block.

The publisher (WP-08) calls `check_publish` immediately before sending and sends exactly
`decision.payload`: hash the bytes it uploads, compare them to the payload, and block on any
difference (CANONICAL_FORM.md, "Publishing").
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.enums import ActorKind, ApprovalSubject
from cortex_operator.contracts.models import Approval, AppUser, AuditEntry, Bundle, Tenant
from cortex_operator.contracts.states import BundleState, ClaimState, VariantState
from cortex_operator.gate.approvals import ACTION_GRANTED, AUDIT_SUBJECT, MAX_APPROVAL_TTL, approver_problem
from cortex_operator.gate.canonical import (
    BundleContent,
    BundleHash,
    LinkResolver,
    PublishPayload,
    VariantContent,
    bundle_hash,
    claim_slots,
    has_unrendered_slot,
    outgoing_text,
    publish_payload,
)
from cortex_operator.gate.repository import GateRepository, MediaUnreadable, load_bundle
from cortex_operator.trust.audit import compute_entry_hash


class BlockReason(StrEnum):
    CANARY = "canary"
    SYSTEM_PAUSED = "system_paused"
    BRAND_PAUSED = "brand_paused"
    NO_BUNDLE = "no_bundle"
    NO_APPROVAL = "no_approval"
    APPROVAL_WRONG_SUBJECT = "approval_wrong_subject"
    APPROVAL_WRONG_TENANT = "approval_wrong_tenant"
    APPROVAL_VOIDED = "approval_voided"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_NOT_YET_VALID = "approval_not_yet_valid"
    APPROVAL_TTL_INVALID = "approval_ttl_invalid"
    APPROVAL_STALE = "approval_stale"
    APPROVER_NOT_ALLOWED = "approver_not_allowed"
    NO_GRANT_RECORD = "no_grant_record"
    CONTENT_CHANGED = "content_changed"
    MEDIA_BYTES_MISMATCH = "media_bytes_mismatch"
    MEDIA_UNREADABLE = "media_unreadable"
    CONSENT_MISSING = "consent_missing"
    CONSENT_WITHDRAWN = "consent_withdrawn"
    CLAIM_NOT_VERIFIED = "claim_not_verified"
    CLAIM_EXPIRED = "claim_expired"
    CLAIM_NO_EVIDENCE = "claim_no_evidence"
    CLAIM_SLOT_UNLINKED = "claim_slot_unlinked"
    UNRENDERED_SLOT = "unrendered_slot"
    UNRESOLVED_LINK = "unresolved_link"
    BUNDLE_NOT_APPROVED = "bundle_not_approved"
    VARIANT_NOT_IN_BUNDLE = "variant_not_in_bundle"
    VARIANT_NOT_PUBLISHABLE = "variant_not_publishable"
    TENANT_MISMATCH = "tenant_mismatch"
    UNHASHABLE = "unhashable"
    REPO_ERROR = "repo_error"
    SPEND_OVER_CAP = "spend_over_cap"
    SPEND_INVALID = "spend_invalid"


PUBLISHABLE_VARIANT_STATES = frozenset({
    VariantState.APPROVED, VariantState.BENCHED, VariantState.SCHEDULED, VariantState.FAILED,
})


@dataclass(frozen=True)
class Block:
    reason: BlockReason
    detail: str


@dataclass(frozen=True)
class SpendCheck:
    """Money this action would commit, in EUR. cap_eur None means no spend is signed."""

    requested_eur: Decimal
    spent_eur: Decimal = Decimal(0)
    cap_eur: Decimal | None = None


@dataclass(frozen=True)
class ApprovalEvidence:
    """The approval row plus what proves it is real: the approver's user row and the audit
    entries about this approval."""

    approval: Approval
    approver: AppUser | None
    audit_entries: tuple[AuditEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    blocks: tuple[Block, ...]
    tenant_id: str
    bundle_id: str
    variant_id: str
    approval_id: str | None = None
    content_hash: str | None = None  # the approved root; goes in PublishRequest.content_hash
    bundle_hash: BundleHash | None = None  # what the content hashes to now
    payload: PublishPayload | None = None  # set only when allowed: send exactly this

    @property
    def reasons(self) -> tuple[BlockReason, ...]:
        return tuple(b.reason for b in self.blocks)

    @property
    def reason(self) -> str:
        return "ok" if self.allowed else self.blocks[0].reason.value

    @property
    def publish_text(self) -> str | None:
        return self.payload.text if self.payload is not None else None

    @property
    def media_sha256(self) -> tuple[str, ...]:
        return self.payload.media_sha256 if self.payload is not None else ()


def _spend_blocks(spend: SpendCheck | None) -> list[Block]:
    if spend is None:
        return []
    values = (spend.requested_eur, spend.spent_eur, spend.cap_eur)
    if any(v is not None and not isinstance(v, Decimal) for v in values):
        return [Block(BlockReason.SPEND_INVALID, "money must be Decimal")]
    if any(v is not None and (not v.is_finite() or v < 0) for v in values):
        return [Block(BlockReason.SPEND_INVALID, "money must be finite and not negative")]
    if spend.requested_eur == 0:
        return []
    if spend.cap_eur is None:
        return [Block(BlockReason.SPEND_OVER_CAP, "no signed spend cap")]
    if spend.spent_eur + spend.requested_eur > spend.cap_eur:
        return [Block(BlockReason.SPEND_OVER_CAP,
                      f"{spend.spent_eur} spent + {spend.requested_eur} requested > cap {spend.cap_eur}")]
    return []


def _claim_blocks(vc: VariantContent, now: datetime) -> list[Block]:
    out: list[Block] = []
    linked = {c.code for c in vc.claims}
    for code in claim_slots(vc.variant.text):
        if code not in linked:
            out.append(Block(BlockReason.CLAIM_SLOT_UNLINKED, f"slot {code} has no linked claim"))
    for c in vc.claims:
        if c.state is not ClaimState.VERIFIED:
            out.append(Block(BlockReason.CLAIM_NOT_VERIFIED, f"claim {c.code} is {c.state.value}"))
        elif c.verified_by is None or c.verified_at is None:
            out.append(Block(BlockReason.CLAIM_NOT_VERIFIED, f"claim {c.code} is marked verified with no verifier"))
        if c.expires_at is not None and c.expires_at <= now:
            out.append(Block(BlockReason.CLAIM_EXPIRED, f"claim {c.code} expired at {c.expires_at.isoformat()}"))
        if not c.evidence_sha256:
            out.append(Block(BlockReason.CLAIM_NO_EVIDENCE, f"claim {c.code} has no evidence hash"))
    return out


def _media_blocks(vc: VariantContent, now: datetime) -> list[Block]:
    out: list[Block] = []
    for m in vc.media:
        if not m.matches_record:
            out.append(Block(BlockReason.MEDIA_BYTES_MISMATCH,
                             f"media {m.media.id}: stored bytes differ from the recorded hash"))
        if m.media.consent_id is None:
            continue
        if m.consent is None or m.consent.id != m.media.consent_id:
            out.append(Block(BlockReason.CONSENT_MISSING, f"media {m.media.id}: consent record missing"))
        elif m.consent.withdrawn_at is not None and m.consent.withdrawn_at <= now:
            out.append(Block(BlockReason.CONSENT_WITHDRAWN, f"media {m.media.id}: consent withdrawn"))
    return out


def _grant_recorded(approval: Approval, entries: Sequence[AuditEntry]) -> bool:
    for e in entries:
        if (e.action == ACTION_GRANTED and e.tenant_id == approval.tenant_id and e.subject_kind == AUDIT_SUBJECT
                and e.subject_id == approval.id and e.actor_kind is ActorKind.HUMAN
                and e.actor_id == approval.approver_id
                and e.payload.get("content_hash") == approval.content_hash
                and e.payload.get("bundle_id") == approval.subject_id
                and compute_entry_hash(e) == e.entry_hash):
            return True
    return False


def _approval_blocks(evidence: ApprovalEvidence | None, bundle: Bundle, now: datetime) -> list[Block]:
    if evidence is None:
        return [Block(BlockReason.NO_APPROVAL, "no approval for this bundle")]
    approval = evidence.approval
    out: list[Block] = []
    if approval.tenant_id != bundle.tenant_id:
        out.append(Block(BlockReason.APPROVAL_WRONG_TENANT, "approval belongs to another tenant"))
    if approval.subject_kind is not ApprovalSubject.BUNDLE or approval.subject_id != bundle.id:
        out.append(Block(BlockReason.APPROVAL_WRONG_SUBJECT, "approval is for another subject"))
    if approval.voided_at is not None:
        out.append(Block(BlockReason.APPROVAL_VOIDED, f"voided: {approval.void_reason or 'no reason given'}"))
    if now >= approval.expires_at:
        out.append(Block(BlockReason.APPROVAL_EXPIRED, f"expired at {approval.expires_at.isoformat()}"))
    if approval.approved_at > now:
        out.append(Block(BlockReason.APPROVAL_NOT_YET_VALID, "approved_at is in the future"))
    life = approval.expires_at - approval.approved_at
    if life.total_seconds() <= 0 or life > MAX_APPROVAL_TTL:
        out.append(Block(BlockReason.APPROVAL_TTL_INVALID, f"lifetime {life} is outside (0, {MAX_APPROVAL_TTL}]"))
    if bundle.root_hash != approval.content_hash:
        out.append(Block(BlockReason.APPROVAL_STALE, "the bundle's current root is not the one this approval signed"))
    if problem := approver_problem(evidence.approver, approval.approver_id, bundle.tenant_id):
        out.append(Block(BlockReason.APPROVER_NOT_ALLOWED, problem))
    if not _grant_recorded(approval, evidence.audit_entries):
        out.append(Block(BlockReason.NO_GRANT_RECORD, "no matching audited grant by the approver"))
    return out


def decide(content: BundleContent, evidence: ApprovalEvidence | None, *, tenant: Tenant, variant_id: str,
           now: datetime, system_paused: bool, resolver: LinkResolver,
           spend: SpendCheck | None = None) -> GateDecision:
    """Allow only if nothing blocks. Every block found is returned, canary first. Never raises."""
    bundle = content.bundle
    approval = evidence.approval if evidence is not None else None
    blocks: list[Block] = []

    # Canary first and unconditionally: a planted flaw never goes out, whatever else holds.
    if any(vc.variant.is_canary for vc in content.variants) or (approval is not None and approval.canary):
        blocks.append(Block(BlockReason.CANARY, "a reviewer canary is never published"))

    if system_paused is not False:  # anything but an explicit False fails closed
        blocks.append(Block(BlockReason.SYSTEM_PAUSED, "global kill switch is on"))
    if tenant.paused:
        blocks.append(Block(BlockReason.BRAND_PAUSED, "brand kill switch is on"))
    if tenant.id != bundle.tenant_id:
        blocks.append(Block(BlockReason.TENANT_MISMATCH, "bundle belongs to another tenant"))
    blocks += [Block(BlockReason.TENANT_MISMATCH, f) for f in content.foreign_rows()]

    if bundle.state is not BundleState.APPROVED:
        blocks.append(Block(BlockReason.BUNDLE_NOT_APPROVED, f"bundle is {bundle.state.value}"))
    target = content.variant(variant_id)
    if target is None:
        blocks.append(Block(BlockReason.VARIANT_NOT_IN_BUNDLE, "variant is not in this bundle"))
    elif target.variant.state not in PUBLISHABLE_VARIANT_STATES:
        blocks.append(Block(BlockReason.VARIANT_NOT_PUBLISHABLE, f"variant is {target.variant.state.value}"))

    blocks += _approval_blocks(evidence, bundle, now)

    for vc in content.variants:
        blocks += _claim_blocks(vc, now)
        blocks += _media_blocks(vc, now)
    if target is not None and has_unrendered_slot(outgoing_text(target.variant)):
        blocks.append(Block(BlockReason.UNRENDERED_SLOT, "outgoing text still has a claim slot"))

    hashed: BundleHash | None = None
    payload: PublishPayload | None = None
    try:
        hashed = bundle_hash(content, resolver)
        if target is not None:
            payload = publish_payload(target, resolver)
    except Exception as exc:  # a hashing failure blocks; it never passes and never raises
        blocks.append(Block(BlockReason.UNHASHABLE, f"{type(exc).__name__}: {exc}"))
    if hashed is not None and approval is not None and hashed.root != approval.content_hash:
        blocks.append(Block(BlockReason.CONTENT_CHANGED, "content no longer matches the approved hash"))
    if payload is not None:
        blocks += [Block(BlockReason.UNRESOLVED_LINK, f"link {url} does not resolve")
                   for url, dest in payload.links if dest is None]

    blocks += _spend_blocks(spend)

    allowed = not blocks
    return GateDecision(
        allowed=allowed, blocks=tuple(blocks), tenant_id=bundle.tenant_id, bundle_id=bundle.id,
        variant_id=variant_id, approval_id=approval.id if approval is not None else None,
        content_hash=approval.content_hash if approval is not None else None, bundle_hash=hashed,
        payload=payload if allowed else None,
    )


def _blocked(tenant_id: str, bundle_id: str, variant_id: str, approval_id: str | None,
             reason: BlockReason, detail: str) -> GateDecision:
    return GateDecision(allowed=False, blocks=(Block(reason, detail),), tenant_id=tenant_id,
                        bundle_id=bundle_id, variant_id=variant_id, approval_id=approval_id)


def check_publish(repo: GateRepository, *, tenant_id: str, bundle_id: str, variant_id: str,
                  approval_id: str | None, clock: Clock, spend: SpendCheck | None = None) -> GateDecision:
    """The one call a publisher makes before sending. Never raises; every failure is a block.
    The global kill switch is read here from the repository and cannot be overridden."""
    def blocked(reason: BlockReason, detail: str) -> GateDecision:
        return _blocked(tenant_id, bundle_id, variant_id, approval_id, reason, detail)

    try:
        paused = repo.system_paused()
        tenant = repo.get_tenant(tenant_id)
        bundle = repo.get_bundle(tenant_id, bundle_id)
        if tenant is None or bundle is None:
            return blocked(BlockReason.NO_BUNDLE, "tenant or bundle not found")
        content = load_bundle(repo, tenant_id, bundle)
        evidence: ApprovalEvidence | None = None
        approval = repo.get_approval(tenant_id, approval_id) if approval_id else None
        if approval is not None:
            evidence = ApprovalEvidence(
                approval=approval, approver=repo.get_user(tenant_id, approval.approver_id),
                audit_entries=tuple(repo.get_audit_entries(tenant_id, AUDIT_SUBJECT, approval.id)))
        now = clock.now()
    except MediaUnreadable as exc:
        return blocked(BlockReason.MEDIA_UNREADABLE, str(exc))
    except Exception as exc:  # any repository failure blocks
        return blocked(BlockReason.REPO_ERROR, f"{type(exc).__name__}: {exc}")
    try:
        return decide(content, evidence, tenant=tenant, variant_id=variant_id, now=now, system_paused=paused,
                      resolver=lambda url: repo.resolve_link(tenant_id, url), spend=spend)
    except Exception as exc:  # belt and braces: decide does not raise, but if it ever does, block
        return blocked(BlockReason.UNHASHABLE, f"{type(exc).__name__}: {exc}")


__all__ = [
    "PUBLISHABLE_VARIANT_STATES", "ApprovalEvidence", "Block", "BlockReason", "GateDecision", "SpendCheck",
    "check_publish", "decide",
]
