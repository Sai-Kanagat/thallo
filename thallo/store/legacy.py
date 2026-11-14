"""SQLite persistence for an Engine's durable state, so passes in separate processes agree.

Legacy: kept so existing imports and tests work unchanged. The Postgres equivalent is
thallo.store.PostgresStore."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from thallo.analytics import Metric
from thallo.contracts import Passport
from thallo.foresight import Entry
from thallo.truth import Claim
from thallo.workflow import Engine

TABLES = ("passports", "drafts", "claims", "taste", "entries", "sealed", "scores",
          "metrics", "progress")
SCHEMA = """
CREATE TABLE IF NOT EXISTS passports (item_id TEXT PRIMARY KEY, approver TEXT, approved_at TEXT,
    content_hash TEXT, rule_version INTEGER, claim_ids TEXT);
CREATE TABLE IF NOT EXISTS drafts (item_id TEXT PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, text TEXT, source TEXT,
    evidence_hash TEXT, expires_at TEXT, verified INTEGER);
CREATE TABLE IF NOT EXISTS taste (id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT);
CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY, at TEXT, previous TEXT, digest TEXT);
CREATE TABLE IF NOT EXISTS sealed (seq INTEGER PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS scores (seq INTEGER PRIMARY KEY, result TEXT);
CREATE TABLE IF NOT EXISTS metrics (n INTEGER PRIMARY KEY, brand TEXT, channel TEXT,
    item_id TEXT, day INTEGER, name TEXT, value REAL, pulled_at TEXT);
CREATE TABLE IF NOT EXISTS progress (item_id TEXT PRIMARY KEY, forecast_seq INTEGER,
    metrics_pulled TEXT);
"""


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_brand(cls, brand: str, folder: str | Path = "state") -> Store:
        return cls(Path(folder) / f"{brand}.db")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.executescript(SCHEMA)
        return db

    def save(self, engine: Engine) -> None:
        taste = engine.taste
        body = {"threshold": taste.threshold, "rules": taste.rules, "version": taste.version,
                "declined": sorted(taste.declined), "removals": dict(taste.removals),
                "graveyard": taste.graveyard, "edit_ratios": taste.edit_ratios}
        progress = getattr(engine.cockpit, "progress", lambda: {})()
        with self._connect() as db:  # one transaction
            for table in TABLES:
                db.execute(f"DELETE FROM {table}")
            db.executemany("INSERT INTO passports VALUES (?,?,?,?,?,?)", [
                (p.item_id, p.approver, p.approved_at.isoformat(), p.content_hash,
                 p.rule_version, json.dumps(p.claim_ids)) for p in engine.passports.values()])
            db.executemany("INSERT INTO drafts VALUES (?,?)", engine.drafts.items())
            db.executemany("INSERT INTO claims VALUES (?,?,?,?,?,?)", [
                (c.id, c.text, c.source, c.evidence_hash,
                 c.expires_at.isoformat() if c.expires_at else None, int(c.verified))
                for c in engine.claims.claims.values()])
            db.execute("INSERT INTO taste VALUES (1, ?)", (json.dumps(body),))
            ledger = engine.forecasts
            db.executemany("INSERT INTO entries VALUES (?,?,?,?)",
                           [(e.seq, e.at, e.previous, e.digest) for e in ledger.entries])
            db.executemany("INSERT INTO sealed VALUES (?,?)",
                           [(s, json.dumps(p)) for s, p in ledger.sealed.items()])
            db.executemany("INSERT INTO scores VALUES (?,?)",
                           [(s, json.dumps(r)) for s, r in ledger.scores.items()])
            db.executemany("INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?)", [
                (n, m.brand, m.channel, m.item_id, m.day, m.name, m.value, m.pulled_at.isoformat())
                for n, m in enumerate(engine.metrics.rows)])
            db.executemany("INSERT INTO progress VALUES (?,?,?)", [
                (i, seq, json.dumps(sorted(pulled))) for i, (seq, pulled) in progress.items()])
        db.close()

    def load(self, engine: Engine) -> None:
        """Replace the engine's durable state in place. A missing file leaves it untouched."""
        if not self.path.exists():
            return
        db = self._connect()
        engine.passports.clear()
        engine.passports.update({r[0]: Passport(r[0], r[1], datetime.fromisoformat(r[2]), r[3],
                                                r[4], tuple(json.loads(r[5])))
                                 for r in db.execute("SELECT * FROM passports")})
        engine.drafts.clear()
        engine.drafts.update(db.execute("SELECT item_id, text FROM drafts").fetchall())
        engine.claims.claims.clear()
        engine.claims.claims.update({r[0]: Claim(r[0], r[1], r[2], r[3], _when(r[4]), bool(r[5]))
                                     for r in db.execute("SELECT * FROM claims")})
        row = db.execute("SELECT body FROM taste").fetchone()
        if row:
            body, taste = json.loads(row[0]), engine.taste
            taste.threshold, taste.version = body["threshold"], body["version"]
            taste.rules[:] = body["rules"]
            taste.declined.clear()
            taste.declined.update(body["declined"])
            taste.removals.clear()
            taste.removals.update(Counter(body["removals"]))
            taste.graveyard[:] = [tuple(pair) for pair in body["graveyard"]]
            taste.edit_ratios[:] = body["edit_ratios"]
        ledger = engine.forecasts
        ledger.entries[:] = [Entry(*r) for r in db.execute(
            "SELECT seq, at, previous, digest FROM entries ORDER BY seq")]
        ledger.sealed.clear()
        ledger.sealed.update({s: json.loads(p) for s, p in db.execute("SELECT * FROM sealed")})
        ledger.scores.clear()
        ledger.scores.update({s: json.loads(r) for s, r in db.execute("SELECT * FROM scores")})
        engine.metrics.rows[:] = [Metric(r[0], r[1], r[2], r[3], r[4], r[5], datetime.fromisoformat(r[6]))
                                  for r in db.execute(
                                      "SELECT brand, channel, item_id, day, name, value, pulled_at "
                                      "FROM metrics ORDER BY n")]
        restore = getattr(engine.cockpit, "restore", None)
        if restore:
            restore({i: (seq, set(json.loads(pulled))) for i, seq, pulled in
                     db.execute("SELECT item_id, forecast_seq, metrics_pulled FROM progress")})
        db.close()
