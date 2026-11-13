"""Fixtures for the wave 1 flow: a throwaway database migrated to head, with a per-run login role
that is a member of cortex_app (the pattern of tests/wp01_store/conftest.py). The Postgres half of
each test is skipped with a reason when DATABASE_URL is unset; the in-memory half always runs."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def pg_urls():
    if not URL:
        pytest.skip("DATABASE_URL unset: the Postgres half runs under scripts/ci.sh")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_int_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(URL)
    params["dbname"] = name
    admin_url = make_conninfo(**params)
    migrate(admin_url)
    password = secrets.token_hex(16)
    role = f"cortex_app_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE cortex_app "
                     "NOSUPERUSER NOBYPASSRLS INHERIT")
    params.update(user=role, password=password)
    try:
        yield {"admin": admin_url, "app": make_conninfo(**params)}
    finally:
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"DROP ROLE IF EXISTS {role}")
