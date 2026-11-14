"""The original engine regression suite (tests/test_store.py), run against the Postgres store,
plus proof that two tenants' engine state never mixes."""
from __future__ import annotations

from datetime import datetime, timedelta

from thallo.contracts import BrandSettings, Item, Status
from thallo.notion import NotionCockpit, NotionMap
from thallo.publishing import MockPublisher
from thallo.store import PostgresStore
from thallo.truth import ClaimLedger
from thallo.workflow import Engine

MONDAY = datetime(2026, 11, 16, 9)  # noqa: DTZ001 naive on purpose: the original engine's own convention
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
                    MockPublisher(), Source(), ClaimLedger())
    return engine, item


def test_approval_survives_into_fresh_engine(database, tenants):
    store = PostgresStore(database, tenants[0].id)
    first, item = build()
    first.claims.add("Born 2026", "doc", "evidence").verified = True
    first.approve(item, "sai", MONDAY - timedelta(days=6))
    store.save(first)

    second, fresh = build(Status.APPROVED)
    store.load(second)
    assert "a" in second.passports
    assert second.passports == first.passports
    assert second.claims.claims.keys() == first.claims.claims.keys()
    assert second.claims.claims == first.claims.claims
    second.tick(MONDAY - timedelta(days=3))
    assert fresh.status is Status.SCHEDULED


def test_forecast_chain_verifies_after_reload(database, tenants):
    store = PostgresStore(database, tenants[0].id)
    first, _ = build()
    first.tick(MONDAY - timedelta(days=7))
    first.forecasts.commit("b", {"impressions": 1.0}, {"impressions": 2.0}, MONDAY)
    first.forecasts.score(1, {"impressions": 400.0})
    store.save(first)

    second, _ = build()
    store.load(second)
    assert len(second.forecasts.entries) == 2
    assert second.forecasts.verify_chain()
    assert second.forecasts.scores == first.forecasts.scores


def test_tampered_reload_fails_verification(database, tenants, admin):
    store = PostgresStore(database, tenants[0].id)
    first, _ = build()
    first.tick(MONDAY - timedelta(days=7))
    store.save(first)
    admin.execute("UPDATE engine_sealed SET payload = replace(payload, '500.0', '999.0')")

    second, _ = build()
    store.load(second)
    assert not second.forecasts.verify_chain()


def test_notion_cache_restores_progress_by_id(database, tenants):
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
    engine = Engine(BrandSettings("sai", {"linkedin"}), first, Writer(), MockPublisher(),
                    Source(), ClaimLedger())
    store = PostgresStore(database, tenants[0].id)
    store.save(engine)

    second = cockpit()
    other = Engine(BrandSettings("sai", {"linkedin"}), second, Writer(), MockPublisher(),
                   Source(), ClaimLedger())
    store.load(other)
    restored = second.items()[0]
    assert restored.forecast_seq == 3 and restored.metrics_pulled == {2}


def test_metrics_round_trip(database, tenants):
    store = PostgresStore(database, tenants[0].id)
    first, _ = build()
    first.metrics.add("sai", "linkedin", "a", 2, {"impressions": 420.0, "clicks": 7.0}, MONDAY)
    store.save(first)
    second, _ = build()
    store.load(second)
    assert second.metrics.rows == first.metrics.rows


def test_two_tenants_engine_state_never_mixes(database, tenants):
    a, b = tenants
    engine_a, item = build()
    engine_a.claims.add("Alpha only", "doc", "evidence").verified = True
    engine_a.approve(item, "sai", MONDAY - timedelta(days=6))
    PostgresStore(database, a.id).save(engine_a)

    engine_b, _ = build()
    PostgresStore(database, b.id).save(engine_b)

    loaded_a, _ = build()
    PostgresStore(database, a.id).load(loaded_a)
    loaded_b, _ = build()
    PostgresStore(database, b.id).load(loaded_b)
    assert "a" in loaded_a.passports and loaded_a.claims.claims
    assert not loaded_b.passports and not loaded_b.claims.claims  # B's save did not touch A, A leaked nothing to B


def test_save_only_replaces_own_rows(database, tenants, admin):
    a, b = tenants
    engine, item = build()
    engine.approve(item, "sai", MONDAY - timedelta(days=6))
    PostgresStore(database, a.id).save(engine)
    PostgresStore(database, b.id).save(build()[0])  # B saves an empty engine: its DELETEs hit only B
    assert admin.execute("SELECT count(*) FROM engine_passport WHERE tenant_id=%s", (a.id,)).fetchone()[0] == 1


def test_load_with_nothing_saved_leaves_engine_untouched(database, tenants):
    engine, item = build()
    engine.drafts["x"] = "keep me"
    PostgresStore(database, tenants[1].id).load(engine)
    assert engine.drafts == {"x": "keep me"}
