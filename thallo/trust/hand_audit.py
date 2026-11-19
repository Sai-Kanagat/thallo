"""The weekly hand audit: 20 sentences from the week's approved pieces, checked by a person.

Counting checkable sentences, not posts, is how a factual error rate under 1% can be shown at
all (BUILD_PLAN T4). The sample is deterministic (a hash order, no random draw), prefers
sentences that state a number, and never includes a canary. The week is the ISO week (Monday,
UTC) in which the variant was made. A sample is topped up to 20 as pieces arrive; positions
already given never change.

A sentence a person marks wrong is a defect that escaped review: the task type goes back to L1
with an incident (`service.record_item`, ESCAPED), the same moment.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from thallo.contracts.actors import Actor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import HumanOnlyAction
from thallo.contracts.ids import new_id
from thallo.contracts.models import Variant
from thallo.contracts.states import VariantState
from thallo.trust import ladder
from thallo.trust import service as svc
from thallo.trust.records import AuditSentence, AuditVerdict, ItemOutcome, TrustItem

SAMPLE = 20
APPROVED_STATES = frozenset({VariantState.APPROVED, VariantState.BENCHED, VariantState.SCHEDULED,
                             VariantState.PUBLISHED, VariantState.READ_BACK_OK, VariantState.READ_BACK_MISMATCH})
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_DIGIT = re.compile(r"\d")


@dataclass(frozen=True)
class WeekSummary:
    week_start: date
    sentences: tuple[AuditSentence, ...]
    checked: int
    wrong: int
    bound_pct: str | None  # 95% upper bound on the error rate per checked sentence
    note: str


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def sentences(text: str) -> list[str]:
    out = []
    for part in _SPLIT.split(text or ""):
        s = part.strip()
        if len(s) >= 12 and any(ch.isalpha() for ch in s):
            out.append(s[:2000])
    return out


def _key(tenant_id: str, week: date, variant_id: str, idx: int) -> str:
    return hashlib.sha256(f"{tenant_id}\n{week.isoformat()}\n{variant_id}\n{idx}".encode()).hexdigest()


def _candidates(tx: Any, week: date) -> list[tuple[bool, str, str, str]]:
    start = datetime.combine(week, time(0), tzinfo=UTC)
    end = start + timedelta(days=7)
    out = []
    for v in tx.list_all(Variant):
        if v.is_canary or v.state not in APPROVED_STATES or not start <= v.created_at < end:
            continue
        for i, s in enumerate(sentences(v.rendered_text or "")):
            out.append((not _DIGIT.search(s), _key(tx.tenant_id, week, v.id, i), v.id, s))
    out.sort()
    return out


def ensure_week(tx: Any, clock: Clock, week_start: date) -> list[AuditSentence]:
    """The week's sample, topped up to 20 from approved pieces not yet sampled."""
    week = monday(week_start)
    have = sorted(tx.list_all(AuditSentence, week_start=week), key=lambda r: r.position)
    if len(have) >= SAMPLE:
        return have
    taken = {(r.variant_id, r.sentence) for r in have}
    pos = len(have)
    now = clock.now()
    for _, _, variant_id, text in _candidates(tx, week):
        if pos >= SAMPLE:
            break
        if (variant_id, text) in taken:
            continue
        pos += 1
        have.append(tx.insert(AuditSentence(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, week_start=week,
                                            position=pos, variant_id=variant_id, sentence=text)))
        taken.add((variant_id, text))
    return have


def summary(tx: Any, clock: Clock, week_start: date) -> WeekSummary:
    rows = tuple(ensure_week(tx, clock, week_start))
    checked = [r for r in rows if r.verdict is not AuditVerdict.PENDING]
    wrong = sum(1 for r in checked if r.verdict is AuditVerdict.WRONG)
    bound = ladder.bound_pct(wrong, len(checked)) if checked else None
    if not rows:
        note = "No approved pieces this week, so there is nothing to audit."
    elif len(rows) < SAMPLE:
        note = f"Only {len(rows)} sentences this week; the sample is short of {SAMPLE}."
    else:
        note = f"{len(checked)} of {SAMPLE} checked."
    if checked:
        note += (f" {wrong} wrong. With {len(checked)} sentences the true error rate could still be up to "
                 f"{bound}% (95% bound); small samples say little on their own.")
    return WeekSummary(monday(week_start), rows, len(checked), wrong, bound, note)


def mark(tx: Any, clock: Clock, sentence_id: str, verdict: AuditVerdict, *, actor: Actor,
         verifier: SessionVerifier, note: str | None = None) -> tuple[AuditSentence, svc.ItemResult | None]:
    """A person checks one sentence against its sources. Wrong is an escaped defect."""
    human = require_human(actor, HumanOnlyAction.VERIFY_CLAIM, verifier=verifier, tenant_id=tx.tenant_id)
    if verdict is AuditVerdict.PENDING:
        raise svc.TrustError("mark a sentence correct or wrong")
    row = tx.get(AuditSentence, sentence_id)
    if row is None:
        raise svc.TrustError("no such sentence")
    if row.verdict is not AuditVerdict.PENDING:
        raise svc.TrustError("this sentence is already checked")
    tx.update(AuditSentence, row.id, verdict=verdict, note=(note or None) and note[:500], checked_by=human.id,
              checked_at=clock.now())
    svc.audit(tx, human, "trust.hand_audit_marked", "trust_audit_sentence", row.id, {"verdict": verdict.value})
    result = None
    if verdict is AuditVerdict.WRONG:
        variant = tx.get(Variant, row.variant_id)
        if variant is not None:
            already = tx.list_all(TrustItem, task_type=variant.task_type, subject_id=variant.id)
            if not (already and already[0].outcome in (ItemOutcome.ESCAPED, ItemOutcome.HARD_RULE)):
                result = svc.record_item(tx, clock, task_type=variant.task_type, subject_kind="variant",
                                         subject_id=variant.id, outcome=ItemOutcome.ESCAPED,
                                         note="weekly hand audit found a wrong sentence")
    return tx.get(AuditSentence, row.id), result


__all__ = ["SAMPLE", "WeekSummary", "ensure_week", "mark", "monday", "sentences", "summary"]
