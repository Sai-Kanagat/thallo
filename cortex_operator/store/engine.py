"""Postgres persistence for an Engine's durable state: same save/load contract as the
SQLite Store, one set of rows per tenant, every statement behind row-level security.

    store = PostgresStore(database, tenant_id)
    store.load(engine)     # a tenant with no saved state leaves the engine untouched
    store.save(engine)     # replaces the tenant's rows in one transaction
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from psycopg.types.json import Jsonb

from cortex_operator.analytics import Metric
from cortex_operator.contracts import Passport
from cortex_operator.foresight import Entry
from cortex_operator.store.db import Database, TenantRepo
from cortex_operator.truth import Claim
from cortex_operator.workflow import Engine

ENGINE_TABLES = ("engine_passport", "engine_draft", "engine_claim", "engine_taste", "engine_entry",
                 "engine_sealed", "engine_score", "engine_metric", "engine_progress")


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class PostgresStore:
    def __init__(self, database: Database, tenant_id: str) -> None:
        self.database = database
        self.tenant_id = tenant_id

    def save(self, engine: Engine) -> None:
        taste = engine.taste
        body = {"threshold": taste.threshold, "rules": taste.rules, "version": taste.version,
                "declined": sorted(taste.declined), "removals": dict(taste.removals),
                "graveyard": taste.graveyard, "edit_ratios": taste.edit_ratios}
        progress = getattr(engine.cockpit, "progress", lambda: {})()
        ledger = engine.forecasts
        with self.database.tenant(self.tenant_id) as repo:  # one transaction
            tid = repo.tenant_id
            for table in ENGINE_TABLES:  # row-level security limits this to the tenant's own rows
                repo.execute(f"DELETE FROM {table}")
            with repo.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO engine_passport VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [(tid, p.item_id, p.approver, p.approved_at.isoformat(), p.content_hash, p.rule_version,
                      Jsonb(list(p.claim_ids))) for p in engine.passports.values()])
                cur.executemany("INSERT INTO engine_draft VALUES (%s,%s,%s)",
                                [(tid, item_id, text) for item_id, text in engine.drafts.items()])
                cur.executemany(
                    "INSERT INTO engine_claim VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [(tid, c.id, c.text, c.source, c.evidence_hash,
                      c.expires_at.isoformat() if c.expires_at else None, c.verified)
                     for c in engine.claims.claims.values()])
                cur.execute("INSERT INTO engine_taste VALUES (%s,%s)", (tid, Jsonb(body)))
                cur.executemany("INSERT INTO engine_entry VALUES (%s,%s,%s,%s,%s)",
                                [(tid, e.seq, e.at, e.previous, e.digest) for e in ledger.entries])
                cur.executemany("INSERT INTO engine_sealed VALUES (%s,%s,%s)",
                                [(tid, s, json.dumps(p)) for s, p in ledger.sealed.items()])
                cur.executemany("INSERT INTO engine_score VALUES (%s,%s,%s)",
                                [(tid, s, json.dumps(r)) for s, r in ledger.scores.items()])
                cur.executemany(
                    "INSERT INTO engine_metric VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(tid, n, m.brand, m.channel, m.item_id, m.day, m.name, m.value,
                      m.pulled_at.isoformat())
                     for n, m in enumerate(engine.metrics.rows)])
                cur.executemany(
                    "INSERT INTO engine_progress VALUES (%s,%s,%s,%s)",
                    [(tid, i, seq, Jsonb(sorted(pulled))) for i, (seq, pulled) in progress.items()])

    def load(self, engine: Engine) -> None:
        """Replace the engine's durable state in place. A tenant with nothing saved leaves it untouched."""
        with self.database.tenant(self.tenant_id) as repo:
            row = repo.execute("SELECT body FROM engine_taste").fetchone()
            if row is None:
                return
            self._restore(repo, engine, row[0])

    @staticmethod
    def _restore(repo: TenantRepo, engine: Engine, taste_body: dict) -> None:
        q = repo.execute
        engine.passports.clear()
        engine.passports.update({
            r[0]: Passport(r[0], r[1], datetime.fromisoformat(r[2]), r[3], r[4], tuple(r[5]))
            for r in q("SELECT item_id, approver, approved_at, content_hash, rule_version, claim_ids "
                       "FROM engine_passport")})
        engine.drafts.clear()
        engine.drafts.update(q("SELECT item_id, body FROM engine_draft").fetchall())
        engine.claims.claims.clear()
        engine.claims.claims.update({
            r[0]: Claim(r[0], r[1], r[2], r[3], _when(r[4]), bool(r[5]))
            for r in q("SELECT claim_id, body, source, evidence_hash, expires_at, verified FROM engine_claim")})
        body, taste = taste_body, engine.taste
        taste.threshold, taste.version = body["threshold"], body["version"]
        taste.rules[:] = body["rules"]
        taste.declined.clear()
        taste.declined.update(body["declined"])
        taste.removals.clear()
        taste.removals.update(Counter(body["removals"]))
        taste.graveyard[:] = [tuple(pair) for pair in body["graveyard"]]
        taste.edit_ratios[:] = body["edit_ratios"]
        ledger = engine.forecasts
        ledger.entries[:] = [Entry(*r) for r in q("SELECT seq, at, previous, digest FROM engine_entry ORDER BY seq")]
        ledger.sealed.clear()
        ledger.sealed.update({s: json.loads(p) for s, p in q("SELECT seq, payload FROM engine_sealed")})
        ledger.scores.clear()
        ledger.scores.update({s: json.loads(r) for s, r in q("SELECT seq, result FROM engine_score")})
        metric_rows = q("SELECT brand, channel, item_id, day, name, value, pulled_at "
                        "FROM engine_metric ORDER BY n")
        engine.metrics.rows[:] = [Metric(*r[:6], datetime.fromisoformat(r[6])) for r in metric_rows]
        restore = getattr(engine.cockpit, "restore", None)
        if restore:
            restore({i: (seq, set(pulled)) for i, seq, pulled in
                     q("SELECT item_id, forecast_seq, metrics_pulled FROM engine_progress")})
