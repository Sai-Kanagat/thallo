from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from thallo.contracts import BrandSettings, Item, Status
from thallo.foresight import ForecastLedger
from thallo.taste import TasteLedger, edit_ratio
from thallo.truth import ClaimLedger, render
from thallo.workflow import DUE, Engine, preflight

MONDAY = datetime(2026, 11, 16, 9)
START = MONDAY - timedelta(days=10)


class Board:
    def __init__(self, items):
        self.list, self.messages = items, []

    def items(self):
        return self.list

    def save(self, item):
        pass

    def notify(self, item, message):
        self.messages.append((item.id, message))


class Writer:
    def __init__(self, text):
        self.text = text

    def draft(self, item, rules):
        return self.text


class Source:
    def pull(self, item):
        return {"impressions": 420.0}


def build(text="We help operators ship calmer weeks.", items=None):
    claims = ClaimLedger()
    items = items or [Item("a", "sai", "linkedin", "brief", MONDAY)]
    board = Board(items)
    engine = Engine(BrandSettings("sai", {"linkedin"}), board, Writer(text),
                    Source(), claims)
    return engine, board, items[0]


def run(engine, start, end, on_day=None):
    now = start
    while now <= end:
        engine.tick(now)
        if on_day:
            on_day(now)
        now += timedelta(hours=6)


def test_full_week_schedules_never_sends_and_scores_once_a_person_marks_it_live():
    """The engine has no send path (WP-08): at the slot it says the item is due, once, and leaves
    it SCHEDULED. A person posts it through the publisher or by hand and marks it Published with
    its link; measurement and forecast scoring then run as before."""
    engine, board, item = build()
    wednesday = MONDAY - timedelta(days=5)

    def approve(now):
        if now >= wednesday and item.status is Status.REVIEW:
            engine.approve(item, "sai", now)

    run(engine, START, MONDAY + timedelta(days=1), approve)
    assert item.status is Status.SCHEDULED and item.url is None
    assert board.messages.count(("a", DUE)) == 1
    item.status, item.url = Status.PUBLISHED, "https://social.invalid/p/1"  # the person marks it live
    run(engine, MONDAY + timedelta(days=1), MONDAY + timedelta(days=8))
    assert item.metrics_pulled == {2, 7}
    assert engine.forecasts.scores[item.forecast_seq]["impressions"]["actual"] == 420.0
    assert engine.forecasts.verify_chain()


def test_missed_approval_goes_on_hold_and_never_posts():
    engine, board, item = build()
    run(engine, START, MONDAY + timedelta(days=2))
    assert item.status is Status.ON_HOLD and item.url is None


def test_unsupported_claim_never_reaches_review():
    engine, board, item = build("We cut review time by 40%.")
    run(engine, START, MONDAY)
    assert item.status is Status.NEEDS_EVIDENCE and item.url is None


def test_verified_claim_compiles_and_expired_claim_does_not():
    ledger = ClaimLedger()
    claim = ledger.add("40% less", "report.pdf", "evidence", MONDAY)
    text = f"We cut review time sharply [[{claim.id}]]."
    assert ledger.compile(text, START)[1]
    ledger.verify(claim.id)
    assert ledger.compile(text, START) == ([claim.id], [])
    assert ledger.compile(text, MONDAY)[1]
    assert "[[" not in render(text)


def approved(text="Calm weeks."):
    engine, board, item = build(text)
    engine.tick(START + timedelta(days=3))
    engine.approve(item, "sai", MONDAY - timedelta(days=5))
    return engine, item


def test_missing_approval_never_schedules():
    engine, board, item = build()
    item.status, item.text = Status.APPROVED, "Calm weeks."
    engine._schedule(item, MONDAY)
    assert item.status is Status.FAILED and ("a", "no approval") in board.messages


def test_changed_content_goes_back_to_review():
    engine, item = approved()
    item.text = "Changed after approval."
    engine.tick(MONDAY - timedelta(days=4))
    assert item.status is Status.REVIEW and item.id not in engine.passports


def test_human_edit_voids_approval():
    engine, item = approved()
    engine.edit(item, "Edited.")
    assert item.status is Status.REVIEW and item.id not in engine.passports


def test_changed_evidence_blocks():
    engine, board, item = build()
    claim = engine.claims.add("x", "s", "old")
    engine.claims.verify(claim.id)
    item.text = f"Fact [[{claim.id}]]."
    passport = engine.approve(item, "sai", START)
    assert passport.content_hash
    claim.evidence_hash = "new"
    engine.tick(START)
    assert item.status is Status.REVIEW and item.id not in engine.passports


@pytest.mark.parametrize(("change", "message"), [
    (lambda b: replace(b, paused=True), "brand paused"), (lambda b: replace(b, channels=set()), "channel not allowed"),
])
def test_pause_and_channel_never_schedule(change, message):
    engine, item = approved()
    board = engine.cockpit
    engine.brand = change(engine.brand)
    engine._schedule(item, MONDAY - timedelta(days=3))
    assert item.status is Status.FAILED and any(message in m for _, m in board.messages)


def test_broken_chain_is_detected():
    ledger = ForecastLedger()
    ledger.commit("a", {"x": 1}, {"x": 0}, START)
    ledger.commit("b", {"x": 2}, {"x": 0}, START)
    assert ledger.verify_chain()
    ledger.sealed[1]["forecast"]["x"] = 99
    assert not ledger.verify_chain()


def test_preflight_catches_channel_errors():
    assert preflight(Item("a", "s", "x", "b", MONDAY, text="y" * 281))
    assert preflight(Item("a", "s", "instagram", "b", MONDAY, text="y"))


def test_repeated_edits_propose_a_rule_for_human_acceptance():
    taste = TasteLedger()
    taste.record_edit("Unlock growth today", "Grow today")
    assert not taste.proposals()
    taste.record_edit("Unlock growth now", "Grow now")
    proposal = taste.proposals()[0]
    assert "unlock growth" in proposal and not taste.rules
    taste.accept(proposal)
    assert taste.version == 1 and not taste.proposals()
    assert edit_ratio("same", "same") == 0


def test_claim_tags_never_reach_the_channel():
    assert render("Fact 1 [[C-0001]].") == "Fact 1."
