"""The publish-time decision (WP-04). The last check before any variant goes out.

`decide` is pure: given a content snapshot (media bytes already hashed), the approval with its
evidence (the approver's user row and the audited grant), the tenant, the global pause and the
time, it returns allow or block with every reason found. `check_publish` loads all of that
through a `GateRepository` and calls `decide`. It never raises: any repository or hashing
failure is a block. Publish also re-reads the check results for the exact content hash of the
payload and blocks on a FAIL, on no CODE-layer result for that content, and on any read error
(gate/checks.py).

The publisher (WP-08) calls `check_publish` immediately before sending and sends exactly
`decision.payload`: hash the bytes it uploads, compare them to the payload, and block on any
difference (CANONICAL_FORM.md, "Publishing").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from thallo.contracts.clock import Clock
from thallo.contracts.enums import ActorKind, ApprovalSubject
from thallo.contracts.models import Approval, AppUser, AuditEntry, Bundle, Tenant
from thallo.contracts.states import BundleState, ClaimState, VariantState
from thallo.gate.approvals import (
    ACTION_GRANTED,
    ACTION_VOIDED,
    AUDIT_SUBJECT,
    MAX_APPROVAL_TTL,
    approver_problem,
)
from thallo.gate.canonical import (
    BundleContent,
    BundleHash,
    LinkResolver,
    PublishPayload,
    VariantContent,
    build_snapshot,
    claim_slots,
    has_unrendered_slot,
    outgoing_text,
)
from thallo.gate.checks import CheckReader, review_checks
from thallo.gate.consent import consent_problem
from thallo.gate.repository import GateRepository, MediaUnreadable, load_bundle
from thallo.gate.signing import GateKeyMissing, GateSigner, grant_fields
from thallo.trust.audit import compute_entry_hash, verify_segment


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
    CONSENT_SCOPE = "consent_scope"
    GATE_KEY_MISSING = "gate_key_missing"
    PAYLOAD_MISMATCH = "payload_mismatch"
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
    CHECK_FAILED = "check_failed"
    NOT_CHECKED = "not_checked"
    CHECKS_UNREADABLE = "checks_unreadable"


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
    chain: tuple[AuditEntry, ...] = field(default_factory=tuple)  # from before the grant through the head
    anchor: tuple[int, str] | None = None  # recorded outside the database (WP-20)


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
        if problem := consent_problem(m, now):
            out.append(Block(BlockReason(problem[0]), problem[1]))
    return out


def _grant_problem(evidence: ApprovalEvidence, signer: GateSigner) -> str | None:
    """None only if an `approval.granted` entry matches the row field for field, carries a valid
    MAC made with the gate key, and sits inside an intact run of the chain through the head (and
    the anchor, when one is recorded)."""
    approval = evidence.approval
    expected = grant_fields(approval)
    candidates = [
        e for e in evidence.audit_entries
        if e.action == ACTION_GRANTED and e.tenant_id == approval.tenant_id and e.subject_kind == AUDIT_SUBJECT
        and e.subject_id == approval.id and e.actor_kind is ActorKind.HUMAN and e.actor_id == approval.approver_id
        and e.payload.get("content_hash") == approval.content_hash
        and e.payload.get("bundle_id") == approval.subject_id
        and e.payload.get("method") == approval.method.value
        and e.payload.get("approved_at") == expected["approved_at"]
        and e.payload.get("expires_at") == expected["expires_at"]
        and e.payload.get("canary") is approval.canary
        and e.payload.get("acknowledged_flag_ids") == expected["acknowledged_flag_ids"]
        and compute_entry_hash(e) == e.entry_hash
    ]
    if not candidates:
        return "no audited grant matches this approval row"
    signed = [e for e in candidates if signer.verify(expected, e.payload.get("mac"))]
    if not signed:
        return "the grant is not signed with the gate key"
    chain = evidence.chain
    report = verify_segment(chain, tenant_id=approval.tenant_id, anchor=evidence.anchor)
    if not report.ok:
        return "the audit chain around the grant is broken: " + "; ".join(p.problem for p in report.problems)
    for e in signed:
        in_chain = any(c.seq == e.seq and c.entry_hash == e.entry_hash for c in chain)
        starts_before = chain[0].seq <= max(1, e.seq - 1)
        if not (in_chain and starts_before):
            continue
        # A void after the grant ends it, whatever the approval row says. Voids are signed, but any
        # void entry counts: voiding only ever removes permission.
        if any(c.seq > e.seq and c.action == ACTION_VOIDED and c.subject_kind == AUDIT_SUBJECT
               and c.subject_id == approval.id for c in chain):
            return "the audit log records a void of this approval after its grant"
        return None
    return "the signed grant is not part of the intact chain"


def _approval_blocks(evidence: ApprovalEvidence | None, bundle: Bundle, now: datetime,
                     signer: GateSigner | None) -> list[Block]:
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
    if signer is None:
        out.append(Block(BlockReason.GATE_KEY_MISSING, "no gate signing key: grants cannot be verified"))
    elif problem := _grant_problem(evidence, signer):
        out.append(Block(BlockReason.NO_GRANT_RECORD, problem))
    return out


def _check_blocks(payload: PublishPayload, *, tenant_id: str, check_reader: CheckReader | None) -> list[Block]:
    """Re-read the check results for exactly the content being sent (gate/checks.py). A missing
    reader or a read that raises blocks: publish fails closed."""
    if check_reader is None:
        return [Block(BlockReason.CHECKS_UNREADABLE, "check results were not read")]
    want = payload.variant_hash
    try:
        rows = list(check_reader(tenant_id, payload.variant_id, want))
        review = review_checks(rows, tenant_id=tenant_id, hashes={payload.variant_id: want})
    except Exception as exc:  # any read failure blocks; it never passes
        return [Block(BlockReason.CHECKS_UNREADABLE, f"{type(exc).__name__}: {exc}")]
    out: list[Block] = []
    if review.unchecked:
        out.append(Block(BlockReason.NOT_CHECKED, "no code check result for the content being sent"))
    if review.failing:
        out.append(Block(BlockReason.CHECK_FAILED, f"check(s) FAIL for this content: {sorted(review.failing)}"))
    # A FLAG raised after the grant blocks nothing here (AUDIT.md): flags are settled at approval.
    return out


def decide(content: BundleContent, evidence: ApprovalEvidence | None, *, tenant: Tenant, variant_id: str,
           now: datetime, system_paused: bool, resolver: LinkResolver, signer: GateSigner | None,
           non_links: frozenset[str] = frozenset(), spend: SpendCheck | None = None,
           check_reader: CheckReader | None = None) -> GateDecision:
    """Allow only if nothing blocks. Every block found is returned, canary first. Never raises.
    `check_reader` reads one variant's check results for one content hash (the repository's
    `get_check_results`); without it publish blocks."""
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

    blocks += _approval_blocks(evidence, bundle, now, signer)

    for vc in content.variants:
        blocks += _claim_blocks(vc, now)
        blocks += _media_blocks(vc, now)
    if target is not None and has_unrendered_slot(outgoing_text(target.variant)):
        blocks.append(Block(BlockReason.UNRENDERED_SLOT, "outgoing text still has a claim slot"))

    hashed: BundleHash | None = None
    payload: PublishPayload | None = None
    unresolved_urls: list[str] = []
    try:
        snapshot = build_snapshot(content, resolver, frozenset(non_links))  # one memoised resolution
        hashed = snapshot.hash
        if target is not None:
            payload = snapshot.payload(variant_id)
            unresolved_urls = snapshot.unresolved(variant_id, frozenset(non_links))  # same allowlist as hashed
    except Exception as exc:  # a hashing failure blocks; it never passes and never raises
        blocks.append(Block(BlockReason.UNHASHABLE, f"{type(exc).__name__}: {exc}"))
    if hashed is not None and approval is not None and hashed.root != approval.content_hash:
        blocks.append(Block(BlockReason.CONTENT_CHANGED, "content no longer matches the approved hash"))
    if payload is not None and (hashed is None or payload.variant_hash != hashed.variants.get(variant_id)):
        blocks.append(Block(BlockReason.PAYLOAD_MISMATCH, "the payload is not the content that was hashed"))
    blocks += [Block(BlockReason.UNRESOLVED_LINK, f"link {url} does not resolve") for url in unresolved_urls]
    if payload is not None:
        blocks += _check_blocks(payload, tenant_id=bundle.tenant_id, check_reader=check_reader)

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
                  approval_id: str | None, clock: Clock, signer: GateSigner | None = None,
                  spend: SpendCheck | None = None) -> GateDecision:
    """The one call a publisher makes before sending. Never raises; every failure is a block.
    The global kill switch is read here from the repository and cannot be overridden. With no
    `signer`, the gate key is read from THALLO_GATE_SIGNING_KEY; if it is missing, publish blocks."""
    def blocked(reason: BlockReason, detail: str) -> GateDecision:
        return _blocked(tenant_id, bundle_id, variant_id, approval_id, reason, detail)

    if signer is None:
        try:
            signer = GateSigner.from_env()
        except GateKeyMissing as exc:
            return blocked(BlockReason.GATE_KEY_MISSING, str(exc))
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
            entries = tuple(repo.get_audit_entries(tenant_id, AUDIT_SUBJECT, approval.id))
            anchor = repo.audit_anchor(tenant_id)
            chain: tuple[AuditEntry, ...] = ()
            if entries:
                start = min([e.seq for e in entries] + ([anchor[0]] if anchor else []))
                chain = tuple(repo.get_audit_chain(tenant_id, max(1, start - 1)))
            evidence = ApprovalEvidence(approval=approval, approver=repo.get_user(tenant_id, approval.approver_id),
                                        audit_entries=entries, chain=chain, anchor=anchor)
        non_links = frozenset(repo.non_link_allowlist(tenant_id))
        now = clock.now()
    except MediaUnreadable as exc:
        return blocked(BlockReason.MEDIA_UNREADABLE, str(exc))
    except Exception as exc:  # any repository failure blocks
        return blocked(BlockReason.REPO_ERROR, f"{type(exc).__name__}: {exc}")
    try:
        return decide(content, evidence, tenant=tenant, variant_id=variant_id, now=now, system_paused=paused,
                      resolver=lambda url: repo.resolve_link(tenant_id, url), signer=signer,
                      non_links=non_links, spend=spend, check_reader=repo.get_check_results)
    except Exception as exc:  # belt and braces: decide does not raise, but if it ever does, block
        return blocked(BlockReason.UNHASHABLE, f"{type(exc).__name__}: {exc}")


__all__ = [
    "PUBLISHABLE_VARIANT_STATES", "ApprovalEvidence", "Block", "BlockReason", "GateDecision", "SpendCheck",
    "check_publish", "decide",
]
