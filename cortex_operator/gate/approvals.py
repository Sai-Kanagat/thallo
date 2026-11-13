"""Approvals: a named, verified human says yes to one exact bundle root hash (WP-04).

Nothing here writes. A grant or a void returns a `GateChange`: the approval rows, the bundle and
variant rows, and the chained audit entries that describe them. The caller (WP-09 for grants,
whoever edits for voids) writes the whole change in ONE transaction, so the log can never say
"granted" or "voided" while the rows say otherwise. See AUDIT.md.

`approve_bundle_from_repo` is the entry point: it loads the bundle through the same repository
path as `check_publish`, so approval and publish resolve links and read media the same way.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from cortex_operator.contracts.actors import Actor, ActorRef, SessionVerifier, require_human
from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.enums import ApprovalMethod, ApprovalSubject, CheckVerdict, HumanOnlyAction, UserRole
from cortex_operator.contracts.ids import is_id, new_id
from cortex_operator.contracts.models import Approval, AppUser, AuditEntry, Bundle, CheckResult, Variant
from cortex_operator.contracts.states import (
    BUNDLE,
    VARIANT,
    BundleState,
    ClaimState,
    IllegalTransition,
    VariantState,
)
from cortex_operator.gate.canonical import BundleContent, BundleHash, LinkResolver, Snapshot, build_snapshot
from cortex_operator.gate.consent import consent_problem
from cortex_operator.gate.repository import GateRepository, load_bundle
from cortex_operator.gate.signing import GateSigner, grant_fields, void_fields
from cortex_operator.trust.audit import AuditDraft, AuditLog

DEFAULT_APPROVAL_TTL = timedelta(days=7)
MAX_APPROVAL_TTL = timedelta(days=30)
APPROVER_ROLES = frozenset({UserRole.FOUNDER, UserRole.APPROVER, UserRole.OPERATOR})

ACTION_GRANTED = "approval.granted"
ACTION_VOIDED = "approval.voided"
AUDIT_SUBJECT = "approval"

# Variant states that hold an approval; a void sends them back through the check stack.
_HOLDS_APPROVAL = frozenset({VariantState.APPROVED, VariantState.BENCHED, VariantState.SCHEDULED,
                             VariantState.FAILED})


class ApprovalRefused(PermissionError):
    pass


@dataclass(frozen=True)
class GateChange:
    """Write all of it in one transaction, or none of it: insert `new_approval` and every
    `audit_entries` row, update every `voided_approvals` row, the `bundle` row and every
    `variants` row. A unique (tenant_id, seq) conflict on the audit insert aborts the whole
    transaction; recompute the change and try again."""

    tenant_id: str
    bundle: Bundle
    variants: tuple[Variant, ...]
    voided_approvals: tuple[Approval, ...]
    audit_entries: tuple[AuditEntry, ...]
    new_approval: Approval | None = None
    bundle_hash: BundleHash | None = None


def open_flag_ids(results: Sequence[CheckResult]) -> frozenset[str]:
    """FLAG verdicts still open: for each (variant, layer, check), the latest attempt is a FLAG."""
    latest: dict[tuple[str, str, str], CheckResult] = {}
    for r in results:
        key = (r.variant_id, r.layer.value, r.check_name)
        if key not in latest or (r.attempt, r.created_at, r.id) > (latest[key].attempt, latest[key].created_at,
                                                                   latest[key].id):
            latest[key] = r
    return frozenset(r.id for r in latest.values() if r.verdict is CheckVerdict.FLAG)


def approver_problem(user: AppUser | None, approver_id: str, tenant_id: str) -> str | None:
    if user is None or user.id != approver_id:
        return "approver is not a known user"
    if user.tenant_id != tenant_id:
        return "approver belongs to another tenant"
    if user.role not in APPROVER_ROLES:
        return f"role {user.role.value} may not approve"
    return None


def _content_problems(content: BundleContent, now: datetime, snapshot: Snapshot,
                      non_links: frozenset[str]) -> list[str]:
    out: list[str] = list(content.foreign_rows())
    for vc in content.variants:
        for c in vc.claims:
            if c.state is not ClaimState.VERIFIED or c.verified_by is None or c.verified_at is None:
                out.append(f"claim {c.code} is not verified by a named person")
            elif c.expires_at is not None and c.expires_at <= now:
                out.append(f"claim {c.code} expired")
            elif not c.evidence_sha256:
                out.append(f"claim {c.code} has no evidence hash")
        for m in vc.media:
            if not m.matches_record:
                out.append(f"media {m.media.id}: stored bytes differ from the recorded hash")
            if problem := consent_problem(m, now):
                out.append(problem[1])
        out += [f"link {url} does not resolve; the reviewer cannot see where it goes"
                for url in snapshot.unresolved(vc.variant.id, non_links)]
    return out


def _void_drafts(approvals: Sequence[Approval], *, reason: str, actor: Actor | ActorRef, now: datetime,
                 signer: GateSigner) -> tuple[list[Approval], list[AuditDraft]]:
    voided: list[Approval] = []
    drafts: list[AuditDraft] = []
    for a in approvals:
        if a.voided_at is not None:
            continue
        voided.append(a.model_copy(update={"voided_at": now, "void_reason": reason}))
        drafts.append(AuditDraft(actor, ACTION_VOIDED, AUDIT_SUBJECT, a.id,
                                 {"reason": reason, "bundle_id": a.subject_id, "content_hash": a.content_hash,
                                  "mac": signer.sign(void_fields(a, reason))}))
    return voided, drafts


def approve_bundle(content: BundleContent, *, actor: Actor, approver: AppUser | None, verifier: SessionVerifier,
                   shown_hash: str, method: ApprovalMethod, seconds_on_screen: float, clock: Clock, audit: AuditLog,
                   resolver: LinkResolver, signer: GateSigner, non_links: frozenset[str] = frozenset(),
                   prior_approvals: Sequence[Approval] = (), ttl: timedelta = DEFAULT_APPROVAL_TTL,
                   check_results: Sequence[CheckResult] = (),
                   acknowledged_flag_ids: Sequence[str] = ()) -> GateChange:
    """A human approves exactly what they saw. Raises HumanOnlyViolation or ApprovalRefused.
    Any earlier live approval of the same bundle is voided in the same change."""
    bundle = content.bundle
    person = require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=bundle.tenant_id)
    if not is_id(person.id):
        raise ApprovalRefused("approver id is not an app_user id")
    if problem := approver_problem(approver, person.id, bundle.tenant_id):
        raise ApprovalRefused(problem)
    if not (isinstance(seconds_on_screen, int | float) and math.isfinite(seconds_on_screen)
            and seconds_on_screen >= 0):
        raise ApprovalRefused("seconds_on_screen must be a finite number, zero or more")
    if not timedelta(0) < ttl <= MAX_APPROVAL_TTL:
        raise ApprovalRefused(f"approval lifetime must be more than zero and at most {MAX_APPROVAL_TTL}")
    if not content.variants:
        raise ApprovalRefused("an empty bundle cannot be approved")
    try:
        BUNDLE.check(bundle.state, BundleState.APPROVED)
        for vc in content.variants:
            VARIANT.check(vc.variant.state, VariantState.APPROVED)
    except IllegalTransition as exc:
        raise ApprovalRefused(f"not in review: {exc}") from exc
    if not isinstance(signer, GateSigner):
        raise ApprovalRefused("no gate signing key: approvals cannot be signed")
    now = clock.now()
    current = {vc.variant.id for vc in content.variants}
    flags = open_flag_ids([r for r in check_results if r.variant_id in current
                           and r.tenant_id == bundle.tenant_id])
    acked = frozenset(acknowledged_flag_ids)
    if unknown := acked - flags:
        raise ApprovalRefused(f"acknowledged ids are not open flags on this bundle: {sorted(unknown)}")
    if missing := flags - acked:
        raise ApprovalRefused(f"{len(missing)} open flag(s) not acknowledged: {sorted(missing)}")
    snapshot = build_snapshot(content, resolver, frozenset(non_links))  # one resolution for checks and hash
    if problems := _content_problems(content, now, snapshot, frozenset(non_links)):
        raise ApprovalRefused("; ".join(problems))
    hashed = snapshot.hash
    if hashed.root != shown_hash:
        raise ApprovalRefused("the bundle changed after it was shown; show it again")
    canary = any(vc.variant.is_canary for vc in content.variants)
    approval = Approval(
        id=new_id(clock), created_at=now, tenant_id=bundle.tenant_id, subject_kind=ApprovalSubject.BUNDLE,
        subject_id=bundle.id, content_hash=hashed.root, approver_id=person.id, method=method,
        seconds_on_screen=float(seconds_on_screen), approved_at=now, expires_at=now + ttl, canary=canary,
        acknowledged_flag_ids=sorted(acked),
    )
    earlier = [a for a in prior_approvals if a.subject_id == bundle.id and a.tenant_id == bundle.tenant_id]
    voided, drafts = _void_drafts(earlier, reason="superseded by a new approval", actor=ActorRef.of(person),
                                  now=now, signer=signer)
    drafts.append(AuditDraft(ActorRef.of(person), ACTION_GRANTED, AUDIT_SUBJECT, approval.id, {
        "bundle_id": bundle.id,
        "content_hash": approval.content_hash,
        "method": method.value,
        "seconds_on_screen": f"{float(seconds_on_screen):.3f}",
        "approved_at": approval.approved_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "canary": canary,
        "acknowledged_flag_ids": sorted(acked),
        "mac": signer.sign(grant_fields(approval)),
    }))
    return GateChange(
        tenant_id=bundle.tenant_id,
        bundle=bundle.model_copy(update={"state": BundleState.APPROVED, "root_hash": hashed.root}),
        variants=tuple(vc.variant.model_copy(update={"state": VariantState.APPROVED,
                                                     "content_sha256": hashed.variants[vc.variant.id]})
                       for vc in content.variants),
        voided_approvals=tuple(voided),
        audit_entries=audit.prepare(bundle.tenant_id, drafts),
        new_approval=approval,
        bundle_hash=hashed,
    )


def approve_bundle_from_repo(repo: GateRepository, *, tenant_id: str, bundle_id: str, actor: Actor,
                             verifier: SessionVerifier, shown_hash: str, method: ApprovalMethod,
                             seconds_on_screen: float, clock: Clock, audit: AuditLog, signer: GateSigner,
                             acknowledged_flag_ids: Sequence[str] = (),
                             ttl: timedelta = DEFAULT_APPROVAL_TTL) -> GateChange:
    """The approval entry point for WP-09. Loads through the same path as `check_publish`."""
    bundle = repo.get_bundle(tenant_id, bundle_id)
    if bundle is None:
        raise ApprovalRefused("bundle not found")
    content = load_bundle(repo, tenant_id, bundle)
    approver = repo.get_user(tenant_id, actor.id)
    return approve_bundle(content, actor=actor, approver=approver, verifier=verifier, shown_hash=shown_hash,
                          method=method, seconds_on_screen=seconds_on_screen, clock=clock, audit=audit,
                          resolver=lambda url: repo.resolve_link(tenant_id, url), signer=signer,
                          non_links=frozenset(repo.non_link_allowlist(tenant_id)),
                          prior_approvals=tuple(repo.get_approvals(tenant_id, bundle_id)), ttl=ttl,
                          check_results=[r for vc in content.variants
                                         for r in repo.get_check_results(tenant_id, vc.variant.id)],
                          acknowledged_flag_ids=acknowledged_flag_ids)


def void_bundle_approvals(bundle: Bundle, variants: Sequence[Variant], approvals: Sequence[Approval], *,
                          reason: str, actor: Actor | ActorRef, clock: Clock, audit: AuditLog,
                          signer: GateSigner) -> GateChange:
    """Void every live approval of a bundle. Call it for ANY edit after a grant (text, media,
    claim, link, rule version), in the same transaction as the edit. Anyone may void: voiding
    only ever removes permission. The bundle goes to VOIDED and approved variants to CHECKING."""
    if not reason or not reason.strip():
        raise ApprovalRefused("a void needs a reason")
    if "session" in reason.lower():
        raise ApprovalRefused("a void reason may not mention a session")
    if not isinstance(signer, GateSigner):
        raise ApprovalRefused("no gate signing key: voids cannot be signed")
    now = clock.now()
    mine = [a for a in approvals if a.subject_id == bundle.id and a.tenant_id == bundle.tenant_id]
    voided, drafts = _void_drafts(mine, reason=reason, actor=actor, now=now, signer=signer)
    new_bundle = bundle
    if bundle.state is BundleState.APPROVED:
        new_bundle = bundle.model_copy(update={"state": BundleState.VOIDED})
    new_variants = []
    for v in variants:
        if v.bundle_id == bundle.id and v.state in _HOLDS_APPROVAL:
            VARIANT.check(v.state, VariantState.CHECKING)
            new_variants.append(v.model_copy(update={"state": VariantState.CHECKING}))
        else:
            new_variants.append(v)
    return GateChange(tenant_id=bundle.tenant_id, bundle=new_bundle, variants=tuple(new_variants),
                      voided_approvals=tuple(voided), audit_entries=audit.prepare(bundle.tenant_id, drafts))


def is_expired(approval: Approval, now: datetime) -> bool:
    return now >= approval.expires_at


__all__ = [
    "ACTION_GRANTED", "ACTION_VOIDED", "APPROVER_ROLES", "AUDIT_SUBJECT", "DEFAULT_APPROVAL_TTL",
    "MAX_APPROVAL_TTL", "ApprovalRefused", "GateChange", "approve_bundle", "approve_bundle_from_repo",
    "approver_problem", "is_expired", "void_bundle_approvals",
]
