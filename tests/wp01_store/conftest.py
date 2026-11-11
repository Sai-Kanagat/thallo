"""Fixtures: a throwaway database, fully migrated, with the app role able to log in.

The app role's password is generated per run and lives only in the throwaway cluster for the
length of the run. Tests are skipped with a reason when DATABASE_URL is unset
(scripts/ci.sh sets it).
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.enums import Language
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import Tenant

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    if URL:
        return
    skip = pytest.mark.skip(reason="DATABASE_URL unset: run scripts/ci.sh for database tests")
    for item in items:
        if "wp01_store" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def urls():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_wp01_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(URL)
    params["dbname"] = name
    admin_url = make_conninfo(**params)
    migrate(admin_url)
    password = secrets.token_hex(16)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE cortex_app LOGIN PASSWORD '{password}'")
    params.update(user="cortex_app", password=password)
    try:
        yield {"admin": admin_url, "app": make_conninfo(**params)}
    finally:
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute("ALTER ROLE cortex_app NOLOGIN PASSWORD NULL")


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def admin(urls):
    import psycopg

    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        yield conn


@pytest.fixture
def database(urls, clock):
    from cortex_operator.store import Database

    return Database(urls["app"], clock, admin_url=urls["admin"])


def make_tenant(database, name: str) -> Tenant:
    clock = database.clock
    clock.advance(seconds=1)
    tenant = Tenant(id=new_id(clock), created_at=clock.now(), name=name,
                    slug=f"{name.lower()}-{secrets.token_hex(3)}", default_language=Language.EN, region_code="IT")
    return database.create_tenant(tenant)


@pytest.fixture
def tenants(database):
    return make_tenant(database, "Alpha"), make_tenant(database, "Beta")
