from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from cortex_operator.contracts import BrandSettings, Item, Status
from cortex_operator.foresight import ForecastLedger
from cortex_operator.publishing import MockPublisher, deliver, preflight
from cortex_operator.taste import TasteLedger, edit_ratio
from cortex_operator.truth import ClaimLedger, render
from cortex_operator.workflow import Engine

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


def build(text="We help operators ship calmer weeks.", items=None, publisher=None):
    claims = ClaimLedger()
    items = items or [Item("a", "sai", "linkedin", "brief", MONDAY)]
    board = Board(items)
    engine = Engine(BrandSettings("sai", {"linkedin"}), board, Writer(text),
                    publisher or MockPublisher(), Source(), claims)
    return engine, board, items[0]


def run(engine, start, end, on_day=None):
    now = start
    while now <= end:
        engine.tick(now)
        if on_day:
            on_day(now)
        now += timedelta(hours=6)


def test_full_week_publishes_and_scores():
    engine, board, item = build()
    wednesday = MONDAY - timedelta(days=5)

    def approve(now):
        if now >= wednesday and item.status is Status.REVIEW:
            engine.approve(item, "sai", now)

    run(engine, START, MONDAY + timedelta(days=8), approve)
    assert item.status is Status.PUBLISHED and item.url
    assert item.metrics_pulled == {2, 7}
    assert engine.forecasts.scores[item.forecast_seq]["impressions"]["actual"] == 420.0
    assert engine.forecasts.verify_chain()
    assert len(engine.publisher.sent) == 1


def test_missed_approval_goes_on_hold_and_never_posts():
    engine, board, item = build()
    run(engine, START, MONDAY + timedelta(days=2))
    assert item.status is Status.ON_HOLD
    assert engine.publisher.calls == 0


def test_unsupported_claim_never_reaches_review():
    engine, board, item = build("We cut review time by 40%.")
    run(engine, START, MONDAY)
    assert item.status is Status.NEEDS_EVIDENCE
    assert engine.publisher.calls == 0


def test_verified_claim_compiles_and_expired_claim_does_not():
    ledger = ClaimLedger()
    claim = ledger.add("40% less", "report.pdf", "evidence", MONDAY)
    text = f"We cut review time by 40% [[{claim.id}]]."
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


def test_missing_approval_blocks():
    engine, board, item = build()
    decision = engine.gate.evaluate(item, None, engine.brand, MONDAY)
    assert not decision.allowed


def test_changed_content_blocks():
    engine, item = approved()
    passport = engine.passports[item.id]
    item.text = "Changed after approval."
    assert not engine.gate.evaluate(item, passport, engine.brand, MONDAY).allowed


def test_human_edit_voids_approval():
    engine, item = approved()
    engine.edit(item, "Edited.")
    assert item.status is Status.REVIEW and item.id not in engine.passports


def test_changed_evidence_blocks():
    engine, board, item = build()
    claim = engine.claims.add("x", "s", "old")
    engine.claims.verify(claim.id)
    item.text = f"Fact 1 [[{claim.id}]]."
    passport = engine.approve(item, "sai", START)
    claim.evidence_hash = "new"
    assert not engine.gate.evaluate(item, passport, engine.brand, START).allowed


def test_kill_switch_pause_channel_and_spend_cap_block():
    engine, item = approved()
    passport = engine.passports[item.id]
    assert engine.gate.evaluate(item, passport, engine.brand, MONDAY).allowed
    assert not engine.gate.evaluate(item, passport, engine.brand, MONDAY, spend_eur=1).allowed
    assert not engine.gate.evaluate(item, passport, replace(engine.brand, paused=True), MONDAY).allowed
    assert not engine.gate.evaluate(item, passport, replace(engine.brand, channels=set()), MONDAY).allowed
    engine.gate.killed = True
    assert not engine.gate.evaluate(item, passport, engine.brand, MONDAY).allowed


def test_broken_chain_is_detected():
    ledger = ForecastLedger()
    ledger.commit("a", {"x": 1}, {"x": 0}, START)
    ledger.commit("b", {"x": 2}, {"x": 0}, START)
    assert ledger.verify_chain()
    ledger.sealed[1]["forecast"]["x"] = 99
    assert not ledger.verify_chain()


def test_delivery_retries_and_never_double_posts():
    item = Item("a", "sai", "linkedin", "b", MONDAY, text="Hi")
    publisher = MockPublisher(failures=2)
    assert deliver(publisher, "k", item)
    assert deliver(publisher, "k", item) == publisher.sent["k"]
    assert len(publisher.sent) == 1
    assert deliver(MockPublisher(failures=3), "k", item) is None


def test_failed_delivery_is_flagged():
    engine, board, item = build(publisher=MockPublisher(failures=9))
    engine.tick(START + timedelta(days=3))
    engine.approve(item, "sai", MONDAY - timedelta(days=5))
    run(engine, MONDAY - timedelta(days=3), MONDAY + timedelta(hours=1))
    assert item.status is Status.FAILED
    assert ("a", "Delivery failed, reschedule needed") in board.messages


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
    publisher = MockPublisher()
    deliver(publisher, "k", Item("a", "s", "linkedin", "b", MONDAY, text="Fact 1 [[C-0001]]."))
    assert publisher.texts == ["Fact 1."]
