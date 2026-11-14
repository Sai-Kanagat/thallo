"""Fixtures: a throwaway database, fully migrated, with the app role able to log in.

The shared cortex_app role is never touched. Each run creates its own login role, a member of
cortex_app (so it holds exactly cortex_app's grants), with a random password, and drops it at the end.
Tests are skipped with a reason when DATABASE_URL is unset
(scripts/ci.sh sets it).
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Language
from thallo.contracts.ids import new_id
from thallo.contracts.models import Operator, Tenant

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
    from thallo.store import Database

    return Database(urls["app"], clock, admin_url=urls["admin"])


def make_tenant(database, name: str) -> Tenant:
    clock = database.clock
    clock.advance(seconds=1)
    op = database.create_operator(Operator(id=new_id(clock), created_at=clock.now(), name=f"op-{name}"))
    tenant = Tenant(id=new_id(clock), created_at=clock.now(), name=name, operator_id=op.id, currency="EUR",
                    slug=f"{name.lower()}-{secrets.token_hex(3)}", default_language=Language.EN, region_code="IT")
    return database.create_tenant(tenant)


@pytest.fixture
def tenants(database):
    return make_tenant(database, "Alpha"), make_tenant(database, "Beta")
