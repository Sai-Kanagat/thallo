"""Edits become rule proposals (BUILD_PLAN 2.7 and module 20). Pure code; time from a Clock.

- Each edit is classified (`diffs.classify`). Typos and fact fixes never make voice rules: a typo is
  not taste, and a fact fix belongs to the truth library.
- Candidate rules come from the diff: a cut makes `AvoidPhrase`, a short swap makes `PreferWording`,
  removed exclamation marks make `MaxExclamations(0)`.
- Two distinct edits with the same candidate make a proposal (the legacy ledger's threshold). A
  rule already live, already proposed or once declined is never proposed again.
- Scope is the narrowest the evidence shares: the pillar when every triggering edit had the same
  pillar, else the channel, else the brand. It widens only through the transfer test.
- Provenance: the triggering edit ids, and the first one as the proposal's regression case.
- The regression check: the rule must break every triggering draft, no triggering final, and no
  final of any case already in the client's set. A human final is the ground truth; a rule that
  flags one contradicts a person and is withheld, with the reason.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from thallo.contracts.clock import Clock
from thallo.contracts.enums import Channel, EditKind, Language, RuleScope
from thallo.contracts.ids import new_id
from thallo.contracts.models import Edit, RuleProposal
from thallo.learning.diffs import EditDiff, classify
from thallo.learning.records import CaseKind, RegressionCase
from thallo.learning.rules import AvoidPhrase, MaxExclamations, PreferWording, Rule, parse_rule

THRESHOLD = 2
MAX_PHRASE_WORDS = 6
NO_RULES = (EditKind.TYPO, EditKind.FACT_FIX)


@dataclass(frozen=True)
class EditContext:
    """An edit and where its piece sat. `pillar` comes from the plan slot (WP-17), if any."""

    edit: Edit
    channel: Channel | None = None
    language: Language | None = None
    pillar: str | None = None


@dataclass(frozen=True)
class Proposed:
    proposal: RuleProposal
    cases: tuple[RegressionCase, ...]  # one per triggering edit, linked to the proposal


@dataclass(frozen=True)
class Withheld:
    rule: Rule
    edit_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ProposalRound:
    proposed: tuple[Proposed, ...]
    withheld: tuple[Withheld, ...]
    kinds: dict[str, EditKind]  # edit id to its kind, for writing back to `edit.kind`


def candidates(diff: EditDiff, draft: str, final: str) -> list[Rule]:
    if diff.kind in NO_RULES:
        return []
    out: list[Rule] = []
    if diff.kind is EditKind.CUT:
        out += [AvoidPhrase(phrase=p) for p in diff.removed if 0 < len(p.split()) <= MAX_PHRASE_WORDS]
    if diff.kind is EditKind.WORD_SWAP:
        out += [PreferWording(avoid=old, use=new) for old, new in diff.swaps]
    if draft.count("!") > 0 and final.count("!") == 0:
        out.append(MaxExclamations(limit=0))
    return out


def _scope(ctxs: Sequence[EditContext]) -> tuple[RuleScope, str | None]:
    pillars = {c.pillar for c in ctxs}
    if len(pillars) == 1 and None not in pillars:
        return RuleScope.PILLAR, pillars.pop()
    channels = {c.channel for c in ctxs}
    if len(channels) == 1 and None not in channels:
        return RuleScope.CHANNEL, channels.pop().value  # type: ignore[union-attr]
    return RuleScope.BRAND, None


def regression_problem(rule: Rule, ctxs: Sequence[EditContext], existing: Iterable[RegressionCase]) -> str | None:
    for c in ctxs:
        if not rule.broken_by(c.edit.before_text):
            return f"does not catch the draft of edit {c.edit.id}"
        if rule.broken_by(c.edit.after_text):
            return f"would flag the human final of edit {c.edit.id}"
    for case in existing:
        if rule.broken_by(case.final):
            return f"would flag the human final of regression case {case.id}"
    return None


def propose(*, tenant_id: str, edits: Sequence[EditContext], existing_cases: Sequence[RegressionCase],
            known: Sequence[RuleProposal], clock: Clock, threshold: int = THRESHOLD) -> ProposalRound:
    edits = [c for c in edits if c.edit.tenant_id == tenant_id]
    existing_cases = [x for x in existing_cases if x.tenant_id == tenant_id]
    seen = {_key(p.rule) for p in known if p.tenant_id == tenant_id}
    used_edits = {eid for p in known if p.tenant_id == tenant_id for eid in p.triggering_edit_ids}
    kinds: dict[str, EditKind] = {}
    groups: dict[str, list[EditContext]] = defaultdict(list)
    rules: dict[str, Rule] = {}
    for c in edits:
        diff = classify(c.edit.before_text, c.edit.after_text)
        kinds[c.edit.id] = diff.kind
        if c.edit.id in used_edits:
            continue
        for rule in candidates(diff, c.edit.before_text, c.edit.after_text):
            k = rule.key()
            if k in seen or any(x.edit.id == c.edit.id for x in groups[k]):
                continue
            rules[k] = rule
            groups[k].append(c)
    proposed: list[Proposed] = []
    withheld: list[Withheld] = []
    for k, ctxs in groups.items():
        if len(ctxs) < threshold:
            continue
        rule = rules[k]
        problem = regression_problem(rule, ctxs, existing_cases)
        if problem:
            withheld.append(Withheld(rule, tuple(c.edit.id for c in ctxs), problem))
            continue
        scope, ref = _scope(ctxs)
        pid = new_id(clock)
        first = ctxs[0].edit
        proposal = RuleProposal(
            id=pid, tenant_id=tenant_id, created_at=clock.now(), scope=scope, scope_ref=ref,
            rule=rule.model_dump(mode="json"), triggering_edit_ids=[c.edit.id for c in ctxs],
            regression_case={"edit_id": first.id, "draft": first.before_text, "final": first.after_text})
        cases = tuple(RegressionCase(
            id=new_id(clock), tenant_id=tenant_id, created_at=clock.now(), kind=CaseKind.REGRESSION,
            edit_id=c.edit.id, rule_proposal_id=pid, scope=scope, scope_ref=ref, channel=c.channel,
            language=c.language, draft=c.edit.before_text, final=c.edit.after_text) for c in ctxs)
        proposed.append(Proposed(proposal, cases))
    return ProposalRound(tuple(proposed), tuple(withheld), kinds)


def _key(rule: dict) -> str:
    return parse_rule(rule).key()


__all__ = ["THRESHOLD", "EditContext", "ProposalRound", "Proposed", "Withheld", "candidates", "propose",
           "regression_problem"]
