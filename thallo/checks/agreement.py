"""Graders are graded (BUILD_PLAN 2.7, last point). Pure code; time comes from a `Clock`.

Once a week, for each grader, every variant the reviewer judged in the window is one item:
- the reviewer found a problem when the variant has an `edit` that is not a typo fix (or carries a
  rejection reason, or counts as a defect);
- the reviewer found it fine when its bundle has an `approval` in the window and no such edit;
- the grader raised a concern when its rows on that variant, at their highest attempt, hold a FLAG
  that is a judgment (a "did not run" or "unreadable" FLAG is not one, and such items are skipped).

Agreement is the share of items where the two match. A grader under `MIN_AGREEMENT` over at least
`MIN_ITEMS` items loses its vote: the stack stops writing its flags (`Settings.dropped_graders`)
and the drop is reported. `check_result` is append-only, so nothing already written is removed or
hidden; graders have no FAILs to drop. A dropped grader is restored by recalibrating it, which
resets its record (a new grader name, or a later window it passes).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from thallo.contracts.clock import Clock
from thallo.contracts.enums import ApprovalSubject, CheckLayer, CheckVerdict, EditKind
from thallo.contracts.models import Approval, CheckResult, Edit

MIN_AGREEMENT = 0.8
MIN_ITEMS = 30
WINDOW = timedelta(days=7)
_NOT_A_JUDGMENT = ("grader did not run", "grader reply was unreadable", "no vision grader", "vision grader not run",
                   "vision grader failed", "the image bytes were not read")


@dataclass(frozen=True)
class GraderScore:
    grader: str
    items: int
    agreed: int
    dropped: bool
    note: str

    @property
    def rate(self) -> float | None:
        return self.agreed / self.items if self.items else None


@dataclass(frozen=True)
class AgreementReport:
    window_start: datetime
    window_end: datetime
    scores: tuple[GraderScore, ...]

    @property
    def dropped(self) -> frozenset[str]:
        return frozenset(s.grader for s in self.scores if s.dropped)

    def lines(self) -> list[str]:
        return [s.note for s in self.scores]


def _in(ts: datetime, start: datetime, end: datetime) -> bool:
    return start <= ts < end


def reviewer_verdicts(edits: Sequence[Edit], approvals: Sequence[Approval], variant_bundles: Mapping[str, str],
                      start: datetime, end: datetime) -> dict[str, bool]:
    """Variant id to True when the reviewer found a problem, False when they approved it unedited."""
    problem: set[str] = set()
    for e in edits:
        if not _in(e.created_at, start, end):
            continue
        if e.counts_as_defect or e.rejection_reason is not None or e.kind not in (None, EditKind.TYPO):
            problem.add(e.variant_id)
        elif e.kind is None and e.before_text != e.after_text:
            problem.add(e.variant_id)
    approved_bundles = {a.subject_id for a in approvals
                        if a.subject_kind is ApprovalSubject.BUNDLE and _in(a.approved_at, start, end)}
    out = {vid: True for vid in problem if vid in variant_bundles}
    for vid, bid in variant_bundles.items():
        if vid not in out and bid in approved_bundles:
            out[vid] = False
    return out


def grader_verdicts(rows: Sequence[CheckResult]) -> dict[tuple[str, str], bool]:
    """(grader, variant id) to True when it raised a judgment FLAG at its highest attempt."""
    top: dict[tuple[str, str], int] = {}
    for r in rows:
        if r.layer is CheckLayer.GRADER:
            key = (r.check_name, r.variant_id)
            top[key] = max(top.get(key, 0), r.attempt)
    out: dict[tuple[str, str], bool] = {}
    skipped: set[tuple[str, str]] = set()
    for r in rows:
        key = (r.check_name, r.variant_id)
        if r.layer is not CheckLayer.GRADER or r.attempt != top[key]:
            continue
        if r.verdict is CheckVerdict.FLAG and (r.reason or "").startswith(_NOT_A_JUDGMENT):
            skipped.add(key)
            continue
        out[key] = out.get(key, False) or r.verdict is CheckVerdict.FLAG
    for key in skipped:
        if not out.get(key):
            out.pop(key, None)  # no judgment made: not an item
    return out


def weekly_agreement(*, grader_rows: Sequence[CheckResult], edits: Sequence[Edit], approvals: Sequence[Approval],
                     variant_bundles: Mapping[str, str], clock: Clock, window: timedelta = WINDOW,
                     min_agreement: float = MIN_AGREEMENT, min_items: int = MIN_ITEMS) -> AgreementReport:
    end = clock.now()
    start = end - window
    reviewer = reviewer_verdicts(edits, approvals, variant_bundles, start, end)
    graded = grader_verdicts(grader_rows)
    names = sorted({g for g, _ in graded})
    scores: list[GraderScore] = []
    for name in names:
        pairs = [(said, reviewer[vid]) for (g, vid), said in graded.items() if g == name and vid in reviewer]
        n, agreed = len(pairs), sum(1 for said, found in pairs if said == found)
        rate = agreed / n if n else None
        dropped = n >= min_items and rate is not None and rate < min_agreement
        if n < min_items:
            note = f"{name}: {n} item(s) this week, fewer than {min_items}: keeps its vote"
        elif dropped:
            note = (f"{name}: agreed with the reviewer on {agreed} of {n} ({rate:.0%}), under {min_agreement:.0%}: "
                    "loses its vote, its flags are dropped until it is recalibrated")
        else:
            note = f"{name}: agreed on {agreed} of {n} ({rate:.0%}): keeps its vote"
        scores.append(GraderScore(name, n, agreed, dropped, note))
    return AgreementReport(start, end, tuple(scores))


__all__ = [
    "MIN_AGREEMENT", "MIN_ITEMS", "WINDOW", "AgreementReport", "GraderScore", "grader_verdicts", "reviewer_verdicts",
    "weekly_agreement",
]
