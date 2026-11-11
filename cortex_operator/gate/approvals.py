"""Approvals: a named, verified human says yes to one exact bundle root hash (WP-04).

`approve_bundle` is the only way to mint a bundle approval. It refuses machines, other tenants
and unverified sessions (via `require_human`), recomputes the root itself and refuses if it
differs from the hash the person was shown, and refuses claims that are not verified or have
expired. Every grant and every void is written to the audit log.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from cortex_operator.contracts.actors import Actor, ActorRef, SessionVerifier, require_human
from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.enums import ApprovalMethod, ApprovalSubject, HumanOnlyAction
from cortex_operator.contracts.ids import is_id, new_id
from cortex_operator.contracts.models import Approval
from cortex_operator.contracts.states import BUNDLE, VARIANT, BundleState, ClaimState, IllegalTransition, VariantState
from cortex_operator.gate.canonical import BundleContent, BundleHash, LinkResolver, bundle_hash
from cortex_operator.trust.audit import AuditLog

DEFAULT_APPROVAL_TTL = timedelta(days=7)
MAX_APPROVAL_TTL = timedelta(days=30)

ACTION_GRANTED = "approval.granted"
ACTION_VOIDED = "approval.voided"


class ApprovalRefused(PermissionError):
    pass


@dataclass(frozen=True)
class Granted:
    """The approval row to store, plus the hashes to write on the bundle (root_hash) and on
    each variant (content_sha256)."""

    approval: Approval
    bundle_hash: BundleHash


def _claim_problems(content: BundleContent, now: datetime) -> list[str]:
    out: list[str] = []
    for vc in content.variants:
        for c in vc.claims:
            if c.state is not ClaimState.VERIFIED:
                out.append(f"claim {c.code} is {c.state.value}, not verified")
            elif c.expires_at is not None and c.expires_at <= now:
                out.append(f"claim {c.code} expired")
            elif not c.evidence_sha256:
                out.append(f"claim {c.code} has no evidence hash")
    return out


def approve_bundle(content: BundleContent, *, actor: Actor, verifier: SessionVerifier, shown_hash: str,
                   method: ApprovalMethod, seconds_on_screen: float, clock: Clock, audit: AuditLog,
                   ttl: timedelta = DEFAULT_APPROVAL_TTL, resolver: LinkResolver | None = None) -> Granted:
    """A human approves exactly what they saw. Raises HumanOnlyViolation or ApprovalRefused."""
    bundle = content.bundle
    person = require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=bundle.tenant_id)
    if not is_id(person.id):
        raise ApprovalRefused("approver id is not an app_user id")
    if not (isinstance(seconds_on_screen, int | float) and math.isfinite(seconds_on_screen)
            and seconds_on_screen >= 0):
        raise ApprovalRefused("seconds_on_screen must be a finite number, zero or more")
    if not timedelta(0) < ttl <= MAX_APPROVAL_TTL:
        raise ApprovalRefused(f"approval lifetime must be more than zero and at most {MAX_APPROVAL_TTL}")
    try:
        BUNDLE.check(bundle.state, BundleState.APPROVED)
        for vc in content.variants:
            VARIANT.check(vc.variant.state, VariantState.APPROVED)
    except IllegalTransition as exc:
        raise ApprovalRefused(f"not in review: {exc}") from exc
    if foreign := content.foreign_rows():
        raise ApprovalRefused("; ".join(foreign))
    if not content.variants:
        raise ApprovalRefused("an empty bundle cannot be approved")
    now = clock.now()
    if problems := _claim_problems(content, now):
        raise ApprovalRefused("; ".join(problems))
    hashed = bundle_hash(content, resolver)
    if hashed.root != shown_hash:
        raise ApprovalRefused("the bundle changed after it was shown; show it again")
    canary = any(vc.variant.is_canary for vc in content.variants)
    approval = Approval(
        id=new_id(clock),
        created_at=now,
        tenant_id=bundle.tenant_id,
        subject_kind=ApprovalSubject.BUNDLE,
        subject_id=bundle.id,
        content_hash=hashed.root,
        approver_id=person.id,
        method=method,
        seconds_on_screen=float(seconds_on_screen),
        approved_at=now,
        expires_at=now + ttl,
        canary=canary,
    )
    audit.append(bundle.tenant_id, ActorRef.of(person), ACTION_GRANTED, subject_kind="approval",
                 subject_id=approval.id, payload={
                     "bundle_id": bundle.id,
                     "content_hash": approval.content_hash,
                     "method": method.value,
                     "seconds_on_screen": f"{float(seconds_on_screen):.3f}",
                     "expires_at": approval.expires_at.isoformat(),
                     "canary": canary,
                 })
    return Granted(approval=approval, bundle_hash=hashed)


def void_approval(approval: Approval, *, reason: str, actor: Actor | ActorRef, clock: Clock,
                  audit: AuditLog) -> Approval:
    """Void an approval (content, claim or rule change, or a person withdrawing it). Anyone may
    void; voiding only ever removes permission."""
    if not reason:
        raise ApprovalRefused("a void needs a reason")
    if approval.voided_at is not None:
        return approval
    now = clock.now()
    voided = approval.model_copy(update={"voided_at": now, "void_reason": reason})
    audit.append(approval.tenant_id, actor, ACTION_VOIDED, subject_kind="approval", subject_id=approval.id,
                 payload={"reason": reason, "content_hash": approval.content_hash})
    return voided


def is_expired(approval: Approval, now: datetime) -> bool:
    return now >= approval.expires_at


__all__ = [
    "ACTION_GRANTED", "ACTION_VOIDED", "DEFAULT_APPROVAL_TTL", "MAX_APPROVAL_TTL", "ApprovalRefused", "Granted",
    "approve_bundle", "is_expired", "void_approval",
]
