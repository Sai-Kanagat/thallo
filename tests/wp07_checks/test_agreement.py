"""Grader agreement with the reviewer: pure code on a fake clock."""
from __future__ import annotations

from datetime import timedelta

from thallo.checks.agreement import weekly_agreement
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import ApprovalMethod, ApprovalSubject, CheckLayer, CheckVerdict, EditKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import Approval, CheckResult, Edit
from thallo.contracts.testing.samples import T

SHA = "b" * 64


def world(n: int, agree: int, *, grader: str = "grader.voice", reason: str = "generic"):
    """n variants; the reviewer approves the even ones unedited and edits the odd ones. The grader
    agrees on the first `agree`."""
    clock = FakeClock(FAKE_EPOCH)
    rows, edits, approvals, bundles = [], [], [], {}
    for i in range(n):
        vid, bid = new_id(clock), new_id(clock)
        bundles[vid] = bid
        problem = i % 2 == 1
        if problem:
            edits.append(Edit(id=new_id(clock), created_at=clock.now(), tenant_id=T, variant_id=vid,
                              editor_id=new_id(clock), before_text="a", after_text="b", kind=EditKind.TONE_SHIFT))
        else:
            approvals.append(Approval(id=new_id(clock), created_at=clock.now(), tenant_id=T,
                                      subject_kind=ApprovalSubject.BUNDLE, subject_id=bid, content_hash=SHA,
                                      approver_id=new_id(clock), method=ApprovalMethod.PER_BUNDLE,
                                      approved_at=clock.now(), expires_at=clock.now() + timedelta(days=1)))
        said = problem if i < agree else not problem
        rows.append(CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=T, variant_id=vid,
                                layer=CheckLayer.GRADER, check_name=grader,
                                verdict=CheckVerdict.FLAG if said else CheckVerdict.PASS,
                                reason=reason if said else None, content_sha256=SHA))
        clock.advance(minutes=10)
    return clock, rows, edits, approvals, bundles


def run(clock, rows, edits, approvals, bundles):
    return weekly_agreement(grader_rows=rows, edits=edits, approvals=approvals, variant_bundles=bundles, clock=clock)


def test_under_80_percent_over_30_items_loses_its_vote():
    report = run(*world(30, 23))
    assert report.dropped == {"grader.voice"} and "loses its vote" in report.lines()[0]


def test_exactly_80_percent_keeps_it_and_fewer_than_30_items_keep_it():
    assert run(*world(30, 24)).dropped == frozenset()
    report = run(*world(29, 0))
    assert report.dropped == frozenset() and "fewer than 30" in report.lines()[0]


def test_only_the_window_counts():
    clock, *rest = world(30, 0)
    clock.advance(days=8)
    assert run(clock, *rest).scores[0].items == 0


def test_a_not_run_row_is_no_judgment_but_model_text_cannot_claim_that():
    clock, rows, edits, approvals, bundles = world(30, 0, reason="grader did not run: spoofed by the model")
    report = run(clock, rows, edits, approvals, bundles)
    assert report.dropped == {"grader.voice"}  # the reason text changes nothing: still judgments
    not_run = [r.model_copy(update={"check_name": "grader.voice.not_run"}) for r in rows]
    report = run(clock, not_run, edits, approvals, bundles)
    assert all(sc.items == 0 for sc in report.scores)
    assert "grader.voice" not in report.dropped
