import sqlite3
from datetime import datetime, timedelta

from thallo.contracts import BrandSettings, Item, Status
from thallo.notion import NotionCockpit, NotionMap
from thallo.store import Store
from thallo.truth import ClaimLedger
from thallo.workflow import Engine

MONDAY = datetime(2026, 11, 16, 9)
TEXT = "We help operators ship calmer weeks."


class Board:
    def __init__(self, items):
        self.list = items

    def items(self):
        return self.list

    def save(self, item):
        pass

    def notify(self, item, message):
        pass


class Writer:
    def draft(self, item, rules):
        return TEXT


class Source:
    def pull(self, item):
        return {"impressions": 420.0}


def build(status=Status.IDEA):
    item = Item("a", "sai", "linkedin", "brief", MONDAY, status, TEXT)
    engine = Engine(BrandSettings("sai", {"linkedin"}), Board([item]), Writer(),
                    Source(), ClaimLedger())
    return engine, item


def test_approval_survives_into_fresh_engine(tmp_path):
    store = Store(tmp_path / "sai.db")
    first, item = build()
    first.claims.add("Born 2026", "doc", "evidence").verified = True
    first.approve(item, "sai", MONDAY - timedelta(days=6))
    store.save(first)

    second, fresh = build(Status.APPROVED)  # the cockpit says Approved; nothing in memory
    store.load(second)
    assert "a" in second.passports
    assert second.claims.claims.keys() == first.claims.claims.keys()
    second.tick(MONDAY - timedelta(days=3))
    assert fresh.status is Status.SCHEDULED


def test_forecast_chain_verifies_after_reload(tmp_path):
    store = Store(tmp_path / "sai.db")
    first, item = build()
    first.tick(MONDAY - timedelta(days=7))
    first.forecasts.commit("b", {"impressions": 1.0}, {"impressions": 2.0}, MONDAY)
    first.forecasts.score(1, {"impressions": 400.0})
    store.save(first)

    second, _ = build()
    store.load(second)
    assert len(second.forecasts.entries) == 2
    assert second.forecasts.verify_chain()
    assert second.forecasts.scores == first.forecasts.scores


def test_tampered_reload_fails_verification(tmp_path):
    store = Store(tmp_path / "sai.db")
    first, _ = build()
    first.tick(MONDAY - timedelta(days=7))
    store.save(first)
    db = sqlite3.connect(tmp_path / "sai.db")
    with db:
        db.execute("UPDATE sealed SET payload = replace(payload, '500.0', '999.0')")
    db.close()

    second, _ = build()
    store.load(second)
    assert not second.forecasts.verify_chain()


def test_notion_cache_restores_progress_by_id(tmp_path):
    def page(date):
        return {"results": [{"id": "p1", "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "brief"}]},
            "Channel": {"select": {"name": "LinkedIn"}},
            "Publish date": {"date": {"start": date}},
            "Status": {"select": {"name": "Published"}},
            "Draft": {"type": "rich_text", "rich_text": [{"plain_text": TEXT}]}}}]}

    def cockpit():
        return NotionCockpit(NotionMap("db", "sai"), lambda *a, **k: page("2026-11-16T09:00:00"))

    first = cockpit()
    item = first.items()[0]
    item.forecast_seq, item.metrics_pulled = 3, {2}
    engine = Engine(BrandSettings("sai", {"linkedin"}), first, Writer(),
                    Source(), ClaimLedger())
    store = Store(tmp_path / "sai.db")
    store.save(engine)

    second = cockpit()
    other = Engine(BrandSettings("sai", {"linkedin"}), second, Writer(),
                   Source(), ClaimLedger())
    store.load(other)
    restored = second.items()[0]
    assert restored.forecast_seq == 3 and restored.metrics_pulled == {2}
