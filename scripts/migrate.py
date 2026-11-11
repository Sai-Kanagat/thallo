"""Apply db/migrations/*.sql in order, once each, recorded in schema_migrations.

    DATABASE_URL=postgresql://... python scripts/migrate.py

Each file runs in its own transaction. A file whose content changed after it was applied
is refused: write a new migration instead of editing an old one.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "db" / "migrations"


def migrate(url: str, directory: Path = MIGRATIONS) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(727274)")  # one runner at a time
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())""")
        done = dict(conn.execute("SELECT name, sha256 FROM schema_migrations").fetchall())
        for path in sorted(directory.glob("*.sql")):
            sql = path.read_text()
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != digest:
                    raise SystemExit(f"{path.name} changed after it was applied; add a new migration")
                continue
            with conn.transaction():
                conn.execute(sql)  # files must not contain their own BEGIN or COMMIT
                conn.execute("INSERT INTO schema_migrations (name, sha256) VALUES (%s, %s)", (path.name, digest))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    for name in migrate(url):
        print(f"applied {name}")
