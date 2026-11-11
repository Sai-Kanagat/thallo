"""The publish-time decision (WP-04). The last check before any variant goes out.

`decide` is pure: given a content snapshot (media bytes already hashed), the approval, the
tenant and the time, it returns allow or block with every reason found. `check_publish` loads
that snapshot through a `GateRepository` (re-reading the media bytes from storage) and calls
`decide`. The publisher (WP-08) calls `check_publish` immediately before sending, and sends
exactly `publish_text` and `media_sha256` from an allowed decision.

Blocks: a canary (always, whatever else is true), a system or brand pause, no approval, an
approval for another bundle, kind or tenant, a voided, expired or not-yet-valid approval,
content that no longer hashes to the approved root (text, media bytes, claim evidence, links,
rule version, channel), media bytes that differ from their record, a claim that is not
verified, expired or without evidence, a claim slot with no linked claim, an unrendered slot
in the outgoing text, a bundle or variant in the wrong state, rows from another tenant, and
spend over the cap.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.enums import ApprovalSubject
from cortex_operator.contracts.models import Approval, Bundle, Claim, Media, Tenant, Variant, VariantMedia
from cortex_operator.contracts.states import BundleState, ClaimState, VariantState
from cortex_operator.gate.canonical import (
    BundleContent,
    BundleHash,
    CanonicalFormError,
    LinkResolver,
    MediaItem,
    VariantContent,
    bundle_hash,
    claim_slots,
    has_unrendered_slot,
    media_hashes,
    outgoing_text,
)


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
    CONTENT_CHANGED = "content_changed"
    MEDIA_BYTES_MISMATCH = "media_bytes_mismatch"
    MEDIA_UNREADABLE = "media_unreadable"
    CLAIM_NOT_VERIFIED = "claim_not_verified"
    CLAIM_EXPIRED = "claim_expired"
    CLAIM_NO_EVIDENCE = "claim_no_evidence"
    CLAIM_SLOT_UNLINKED = "claim_slot_unlinked"
    UNRENDERED_SLOT = "unrendered_slot"
    BUNDLE_NOT_APPROVED = "bundle_not_approved"
    VARIANT_NOT_IN_BUNDLE = "variant_not_in_bundle"
    VARIANT_NOT_PUBLISHABLE = "variant_not_publishable"
    TENANT_MISMATCH = "tenant_mismatch"
    UNHASHABLE = "unhashable"
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
class GateDecision:
    allowed: bool
    blocks: tuple[Block, ...]
    tenant_id: str
    bundle_id: str
    variant_id: str
    approval_id: str | None = None
    content_hash: str | None = None  # the approved root; goes in PublishRequest.content_hash
    bundle_hash: BundleHash | None = None  # what the content hashes to now
    publish_text: str | None = None  # set only when allowed: send exactly this
    media_sha256: tuple[str, ...] = ()  # set only when allowed

    @property
    def reasons(self) -> tuple[BlockReason, ...]:
        return tuple(b.reason for b in self.blocks)

    @property
    def reason(self) -> str:
        return "ok" if self.allowed else self.blocks[0].reason.value


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
        if c.expires_at is not None and c.expires_at <= now:
            out.append(Block(BlockReason.CLAIM_EXPIRED, f"claim {c.code} expired at {c.expires_at.isoformat()}"))
        if not c.evidence_sha256:
            out.append(Block(BlockReason.CLAIM_NO_EVIDENCE, f"claim {c.code} has no evidence hash"))
    return out


def _approval_blocks(approval: Approval | None, bundle: Bundle, now: datetime) -> list[Block]:
    if approval is None:
        return [Block(BlockReason.NO_APPROVAL, "no approval for this bundle")]
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
    return out


def decide(content: BundleContent, approval: Approval | None, *, tenant: Tenant, variant_id: str,
           now: datetime, spend: SpendCheck | None = None, resolver: LinkResolver | None = None,
           system_paused: bool = False) -> GateDecision:
    """Allow only if nothing blocks. Every block found is returned, canary first."""
    bundle = content.bundle
    blocks: list[Block] = []

    # Canary first and unconditionally: a planted flaw never goes out, whatever else holds.
    canaries = [vc.variant.id for vc in content.variants if vc.variant.is_canary]
    if canaries or (approval is not None and approval.canary):
        blocks.append(Block(BlockReason.CANARY, "a reviewer canary is never published"))

    if system_paused:
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

    blocks += _approval_blocks(approval, bundle, now)

    for vc in content.variants:
        blocks += _claim_blocks(vc, now)
        for m in vc.media:
            if not m.matches_record:
                blocks.append(Block(BlockReason.MEDIA_BYTES_MISMATCH,
                                    f"media {m.media.id}: stored bytes differ from the recorded hash"))
    if target is not None and has_unrendered_slot(outgoing_text(target.variant)):
        blocks.append(Block(BlockReason.UNRENDERED_SLOT, "outgoing text still has a claim slot"))

    hashed: BundleHash | None = None
    try:
        hashed = bundle_hash(content, resolver)
    except CanonicalFormError as exc:
        blocks.append(Block(BlockReason.UNHASHABLE, str(exc)))
    if hashed is not None and approval is not None and hashed.root != approval.content_hash:
        blocks.append(Block(BlockReason.CONTENT_CHANGED, "content no longer matches the approved hash"))

    blocks += _spend_blocks(spend)

    allowed = not blocks
    return GateDecision(
        allowed=allowed,
        blocks=tuple(blocks),
        tenant_id=bundle.tenant_id,
        bundle_id=bundle.id,
        variant_id=variant_id,
        approval_id=approval.id if approval is not None else None,
        content_hash=approval.content_hash if approval is not None else None,
        bundle_hash=hashed,
        publish_text=outgoing_text(target.variant) if allowed and target is not None else None,
        media_sha256=media_hashes(target) if allowed and target is not None else (),
    )


# ---------------------------------------------------------------- loading through a repository

class GateRepository(Protocol):
    """What the gate reads. WP-01 implements it; every call is tenant-scoped."""

    def get_tenant(self, tenant_id: str) -> Tenant | None: ...

    def get_bundle(self, tenant_id: str, bundle_id: str) -> Bundle | None: ...

    def get_variants(self, tenant_id: str, bundle_id: str) -> Sequence[Variant]: ...

    def get_variant_media(self, tenant_id: str, variant_id: str) -> Sequence[tuple[VariantMedia, Media]]: ...

    def read_media_bytes(self, tenant_id: str, media: Media) -> bytes | Iterable[bytes]:
        """The stored file's current bytes, read fresh from storage (not a cached hash)."""
        ...

    def get_variant_claims(self, tenant_id: str, variant_id: str) -> Sequence[Claim]: ...

    def get_approval(self, tenant_id: str, approval_id: str) -> Approval | None: ...

    def resolve_link(self, tenant_id: str, url: str) -> str | None:
        """Final destination of a link (tracked link table, known redirects). No network."""
        ...


class MediaUnreadable(RuntimeError):
    pass


def load_bundle(repo: GateRepository, tenant_id: str, bundle: Bundle) -> BundleContent:
    """Snapshot a bundle, hashing each media file from its bytes as read now."""
    variants: list[VariantContent] = []
    for v in repo.get_variants(tenant_id, bundle.id):
        items: list[MediaItem] = []
        for link, media in repo.get_variant_media(tenant_id, v.id):
            try:
                data = repo.read_media_bytes(tenant_id, media)
                items.append(MediaItem.from_bytes(media, link.position, data))
            except Exception as exc:  # any storage failure blocks; it never passes
                raise MediaUnreadable(f"media {media.id}: {type(exc).__name__}") from exc
        variants.append(VariantContent(variant=v, media=tuple(items),
                                       claims=tuple(repo.get_variant_claims(tenant_id, v.id))))
    return BundleContent(bundle=bundle, variants=tuple(variants))


def _blocked(tenant_id: str, bundle_id: str, variant_id: str, approval_id: str | None,
             reason: BlockReason, detail: str) -> GateDecision:
    return GateDecision(allowed=False, blocks=(Block(reason, detail),), tenant_id=tenant_id,
                        bundle_id=bundle_id, variant_id=variant_id, approval_id=approval_id)


def check_publish(repo: GateRepository, *, tenant_id: str, bundle_id: str, variant_id: str,
                  approval_id: str | None, clock: Clock, spend: SpendCheck | None = None,
                  system_paused: bool = False) -> GateDecision:
    """The one call a publisher makes before sending. Never raises for a missing row; blocks."""
    tenant = repo.get_tenant(tenant_id)
    bundle = repo.get_bundle(tenant_id, bundle_id)
    if tenant is None or bundle is None:
        return _blocked(tenant_id, bundle_id, variant_id, approval_id, BlockReason.NO_BUNDLE,
                        "tenant or bundle not found")
    try:
        content = load_bundle(repo, tenant_id, bundle)
    except MediaUnreadable as exc:
        return _blocked(tenant_id, bundle_id, variant_id, approval_id, BlockReason.MEDIA_UNREADABLE, str(exc))
    approval = repo.get_approval(tenant_id, approval_id) if approval_id else None
    return decide(content, approval, tenant=tenant, variant_id=variant_id, now=clock.now(), spend=spend,
                  resolver=lambda url: repo.resolve_link(tenant_id, url), system_paused=system_paused)


__all__ = [
    "PUBLISHABLE_VARIANT_STATES", "Block", "BlockReason", "GateDecision", "GateRepository", "MediaUnreadable",
    "SpendCheck", "check_publish", "decide", "load_bundle",
]
