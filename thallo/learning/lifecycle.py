"""A rule's life: proposed, accepted by a person, promoted after the transfer test, expired when
unused for 90 days (BUILD_PLAN 2.7, module 20). Pure code; time from a Clock.

- Accepting and promoting are human-only (`require_human` with a SessionVerifier). Accepting writes
  an `approval` row with subject `rule_proposal`, bound to the hash of the rule and its scope, and
  only then a `voice_rule`. Nothing applies before a person said yes.
- "Used" means the rule caught something on a draft, not that it was loaded into a prompt; else
  nothing would ever expire. `record_use` moves `last_used_at` only when the rule fires.
- The transfer test reads fresh edits made after acceptance outside the rule's scope. A hit is an
  edit whose draft broke the rule and whose human final does not: the person made the change the
  rule would have made. A contradiction is a human final that breaks it. Promotion needs at least
  `TRANSFER_HITS` hits in at least two places outside the scope and no contradiction.
- Expiry is legal only from accepted or promoted, after `EXPIRY` without use, counted from the last
  use or from acceptance.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from thallo.contracts.actors import Actor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import ApprovalMethod, ApprovalSubject, HumanOnlyAction, RuleScope
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Approval, RuleProposal, VoiceRule
from thallo.contracts.states import RULE_PROPOSAL, RuleProposalState
from thallo.gate.approvals import approver_problem
from thallo.learning.proposals import EditContext
from thallo.learning.rules import parse_rule

EXPIRY = timedelta(days=90)
TRANSFER_HITS = 2
S = RuleProposalState


class LifecycleError(ValueError):
    pass


def rule_hash(proposal: RuleProposal) -> str:
    body = {"rule": proposal.rule, "scope": proposal.scope.value, "scope_ref": proposal.scope_ref}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _move(p: RuleProposal, target: S) -> RuleProposal:
    RULE_PROPOSAL.check(p.state, target)
    return p.model_copy(update={"state": target})


@dataclass(frozen=True)
class Accepted:
    proposal: RuleProposal
    voice_rule: VoiceRule
    approval: Approval


def accept(proposal: RuleProposal, *, actor: Actor, verifier: SessionVerifier, approver: AppUser | None,
           clock: Clock, seconds_on_screen: float | None = None) -> Accepted:
    """The caller writes the three rows in one transaction. The approver recorded is the verified
    person, never a separate argument; `approver` is that person's membership row, checked with the
    same role rule the bundle gate uses."""
    person = require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=proposal.tenant_id)
    if problem := approver_problem(approver, person.id, proposal.tenant_id):
        raise LifecycleError(problem)
    moved = _move(proposal, S.ACCEPTED)
    now = clock.now()
    approval = Approval(
        id=new_id(clock), tenant_id=proposal.tenant_id, created_at=now, subject_kind=ApprovalSubject.RULE_PROPOSAL,
        subject_id=proposal.id, content_hash=rule_hash(proposal), approver_id=person.id,
        method=ApprovalMethod.PER_BUNDLE, seconds_on_screen=seconds_on_screen, approved_at=now,
        expires_at=now + EXPIRY)
    rule = VoiceRule(
        id=new_id(clock), tenant_id=proposal.tenant_id, created_at=now, rule_proposal_id=proposal.id,
        scope=proposal.scope, scope_ref=proposal.scope_ref, body=proposal.rule, expires_at=now + EXPIRY)
    return Accepted(moved.model_copy(update={"expires_at": now + EXPIRY}), rule, approval)


def reject(proposal: RuleProposal, *, actor: Actor, verifier: SessionVerifier) -> RuleProposal:
    require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=proposal.tenant_id)
    return _move(proposal, S.REJECTED)


def in_scope(scope: RuleScope, scope_ref: str | None, ctx: EditContext) -> bool:
    if scope is RuleScope.BRAND:
        return True
    if scope is RuleScope.PILLAR:
        return ctx.pillar == scope_ref
    return ctx.channel is not None and ctx.channel.value == scope_ref


def live_rules(rules: Iterable[VoiceRule], ctx: EditContext, clock: Clock) -> list[VoiceRule]:
    now = clock.now()
    return [r for r in rules if r.tenant_id == ctx.edit.tenant_id and (r.expires_at is None or r.expires_at > now)
            and in_scope(r.scope, r.scope_ref, ctx)]


def record_use(rule: VoiceRule, draft: str, clock: Clock) -> VoiceRule:
    """A rule counts as used only when it caught something on a draft."""
    if not parse_rule(rule.body).broken_by(draft):
        return rule
    now = clock.now()
    return rule.model_copy(update={"last_used_at": now, "expires_at": now + EXPIRY})


@dataclass(frozen=True)
class TransferResult:
    hits: tuple[str, ...]
    contradictions: tuple[str, ...]
    places: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return len(self.hits) >= TRANSFER_HITS and len(set(self.places)) >= 2 and not self.contradictions


def _place(ctx: EditContext) -> str:
    return f"pillar:{ctx.pillar}" if ctx.pillar else f"channel:{ctx.channel.value if ctx.channel else '-'}"


def transfer_test(proposal: RuleProposal, accepted_rule: VoiceRule, fresh: Sequence[EditContext]) -> TransferResult:
    rule = parse_rule(proposal.rule)
    hits, contra, places = [], [], []
    for ctx in fresh:
        e = ctx.edit
        if e.tenant_id != proposal.tenant_id or e.created_at <= accepted_rule.created_at \
                or e.id in proposal.triggering_edit_ids or in_scope(proposal.scope, proposal.scope_ref, ctx):
            continue
        if rule.broken_by(e.after_text):
            contra.append(e.id)
        elif rule.broken_by(e.before_text):
            hits.append(e.id)
            places.append(_place(ctx))
    return TransferResult(tuple(hits), tuple(contra), tuple(places))


def promote(proposal: RuleProposal, rule: VoiceRule, result: TransferResult, *, actor: Actor,
            verifier: SessionVerifier, clock: Clock) -> tuple[RuleProposal, VoiceRule]:
    """Widening where a rule applies is a person's call, and only after the transfer test passed."""
    require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=proposal.tenant_id)
    if not result.passed:
        raise LifecycleError(f"transfer test not passed: {len(result.hits)} hit(s), "
                             f"{len(result.contradictions)} contradiction(s)")
    moved = _move(proposal, S.PROMOTED).model_copy(update={"scope": RuleScope.BRAND, "scope_ref": None})
    now = clock.now()
    widened = rule.model_copy(update={"scope": RuleScope.BRAND, "scope_ref": None, "last_used_at": now,
                                      "expires_at": now + EXPIRY})
    return moved, widened


def expire_due(proposal: RuleProposal, rule: VoiceRule, clock: Clock) -> RuleProposal | None:
    """The expired proposal when 90 days passed without use, else None."""
    if proposal.state not in (S.ACCEPTED, S.PROMOTED):
        return None
    since = rule.last_used_at or rule.created_at
    if clock.now() - since < EXPIRY:
        return None
    return _move(proposal, S.EXPIRED)


__all__ = ["EXPIRY", "TRANSFER_HITS", "Accepted", "LifecycleError", "TransferResult", "accept", "expire_due",
           "in_scope", "live_rules", "promote", "record_use", "reject", "rule_hash", "transfer_test"]
