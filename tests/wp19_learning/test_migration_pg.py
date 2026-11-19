"""Migration 0032 on a throwaway database: tables exist, RLS is on, a passed run must be clean."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

URL = os.environ.get("DATABASE_URL")
ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")
TABLES = {"regression_case", "eval_run", "blind_item", "blind_score"}


@pytest.fixture(autouse=True)
def no_network():  # this test talks to the local Postgres on purpose
    yield


@pytest.fixture
def admin():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_wp19_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(URL)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        migrate(url)
        with psycopg.connect(url, autocommit=True) as conn:
            yield conn
    finally:
        with psycopg.connect(URL, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_tables_have_rls_and_a_pass_must_be_clean(admin):
    import psycopg

    rls = {r[0] for r in admin.execute("SELECT relname FROM pg_class WHERE relrowsecurity").fetchall()}
    assert TABLES <= rls
    op = "0190f3a0-0000-7000-8000-0000000000f0"
    t = "0190f3a0-0000-7000-8000-000000000001"
    admin.execute("INSERT INTO operator (id, created_at, name) VALUES (%s, now(), 'op')", (op,))
    admin.execute("INSERT INTO tenant (id, created_at, name, slug, region_code, operator_id, currency, "
                  "default_language) VALUES (%s, now(), 't', 't', 'IT', %s, 'EUR', 'en')", (t, op))
    sql = ("INSERT INTO eval_run (id, created_at, tenant_id, change_kind, change_name, change_sha256, "
           "case_set_sha256, cases, failed, flagged, not_run, passed, ran_at) "
           "VALUES (gen_random_uuid(), now(), %s, 'prompt', 'draft@2', %s, %s, %s, %s, 0, 0, true, now())")
    admin.execute(sql, (t, "a" * 64, "b" * 64, 2, 0))
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute(sql, (t, "a" * 64, "b" * 64, 2, 1))
