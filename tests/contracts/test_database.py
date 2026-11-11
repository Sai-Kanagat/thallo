"""The migration on a real Postgres. Skipped with a clear reason when DATABASE_URL is unset.

scripts/ci.sh starts a throwaway Postgres in Docker and sets DATABASE_URL, so CI runs these.
Each run creates and drops its own database, so it never touches data you care about.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")

T = "0190f3a0-0000-7000-8000-000000000001"
U = "0190f3a0-0000-7000-8000-000000000002"
P = "0190f3a0-0000-7000-8000-000000000003"


@pytest.fixture(scope="module")
def db():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_ct_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(URL)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        assert migrate(url)[0] == "0001_core.sql"  # later packages add numbered migrations after 0001
        assert migrate(url) == []  # idempotent
        with psycopg.connect(url, autocommit=True) as conn:
            conn.test_url = url  # type: ignore[attr-defined]
            yield conn
    finally:
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_all_tables_exist(db):
    from cortex_operator.contracts.models import TABLES

    rows = db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()
    assert {t.__table__ for t in TABLES} <= {r[0] for r in rows}


def test_model_columns_match_database(db):
    from cortex_operator.contracts.models import TABLES

    for t in TABLES:
        cols = db.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s",
                          (t.__table__,)).fetchall()
        assert {c[0] for c in cols} == set(t.model_fields), t.__table__


def test_constraints_hold(db):
    import psycopg

    db.execute("INSERT INTO tenant (id, created_at, name, slug, default_language, region_code) "
               "VALUES (%s, now(), 'Demo', 'demo', 'en', 'IT')", (T,))
    db.execute("INSERT INTO person (id, created_at, tenant_id) VALUES (%s, now(), %s)", (P, T))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO lead (id, created_at, tenant_id, person_id, fit) VALUES (%s, now(), %s, %s, 101)",
                   (U, T, P))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO lead (id, created_at, tenant_id, person_id, state) VALUES (%s, now(), %s, %s, 'x')",
                   (U, T, P))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("INSERT INTO lead (id, created_at, tenant_id, person_id) VALUES (%s, now(), %s, %s)", (U, T, U))
    db.execute("INSERT INTO lead (id, created_at, tenant_id, person_id) VALUES (%s, now(), %s, %s)", (U, T, P))
    state, fit, answers = db.execute("SELECT state, fit, answers FROM lead WHERE id=%s", (U,)).fetchone()
    assert (state, fit, answers) == ("new", 0, {})


def test_changed_migration_is_refused(db, tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    (tmp_path / "0001_core.sql").write_text("SELECT 1;\n")
    url = db.test_url
    with pytest.raises(SystemExit):
        migrate(url, tmp_path)


def test_rows_cannot_point_at_another_tenant(db):
    import psycopg

    t2, p2, lead = ("0190f3a0-0000-7000-8000-0000000000a1", "0190f3a0-0000-7000-8000-0000000000a2",
                    "0190f3a0-0000-7000-8000-0000000000a3")
    db.execute("INSERT INTO tenant (id, created_at, name, slug, default_language, region_code) "
               "VALUES (%s, now(), 'Other', 'other', 'it', 'IT') ON CONFLICT DO NOTHING", (t2,))
    db.execute("INSERT INTO person (id, created_at, tenant_id) VALUES (%s, now(), %s) ON CONFLICT DO NOTHING",
               (p2, t2))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # tenant A lead on tenant B's person
        db.execute("INSERT INTO lead (id, created_at, tenant_id, person_id) VALUES (%s, now(), %s, %s)",
                   (lead, T, p2))
