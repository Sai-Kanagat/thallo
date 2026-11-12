"""Postgres stores, tested as the application role (a per-run login role that inherits cortex_app).

Skipped when DATABASE_URL is unset: scripts/ci.sh sets it.
"""
from __future__ import annotations

import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path

import pytest

URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")

ROOT = Path(__file__).resolve().parents[2]
PLAIN = "sk-test-PGPLAIN-31337"


@pytest.fixture(scope="module")
def urls():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_wp02_{secrets.token_hex(4)}"
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
def admin(urls):
    import psycopg

    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        yield conn


@pytest.fixture
def clock():
    from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock

    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def database(urls, clock):
    from cortex_operator.store import Database

    return Database(urls["app"], clock, admin_url=urls["admin"])


@pytest.fixture
def tenants(database):
    from cortex_operator.contracts.enums import Language
    from cortex_operator.contracts.ids import new_id
    from cortex_operator.contracts.models import Tenant

    made = []
    for name in ("Alpha", "Beta"):
        database.clock.advance(seconds=1)
        made.append(database.create_tenant(Tenant(
            id=new_id(database.clock), created_at=database.clock.now(), name=name,
            slug=f"{name.lower()}-{secrets.token_hex(3)}", default_language=Language.EN, region_code="IT")).id)
    return made


@pytest.fixture
def stores(database, tenants):
    from cortex_operator.connections.pg import (
        PostgresChannelAccountStore,
        PostgresConnectionStore,
        PostgresHealthState,
        PostgresSecretStore,
    )

    ids = lambda: list(tenants)  # noqa: E731
    return {
        "secrets": PostgresSecretStore(database, ids), "connections": PostgresConnectionStore(database, ids),
        "accounts": PostgresChannelAccountStore(database, ids), "health": PostgresHealthState(database),
    }


@pytest.fixture
def vault(stores, clock):
    from cortex_operator.connections import KeyRing, Vault

    return Vault(KeyRing(os.urandom(32)), stores["secrets"], clock)


# ---------------------------------------------------------------- vault

def test_secret_round_trips_and_the_table_holds_no_plaintext(vault, admin, tenants):
    a, _ = tenants
    ref = vault.put(a, PLAIN)
    assert vault.get(a, ref).reveal() == PLAIN
    dump = admin.execute("SELECT t::text FROM vault_secret t").fetchall()
    assert dump and all(PLAIN not in row[0] and PLAIN.encode().hex() not in row[0] for row in dump)
    cols = {r[0] for r in admin.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'vault_secret'").fetchall()}
    assert cols == {"tenant_id", "ref", "key_id", "version", "nonce", "ciphertext", "created_at", "updated_at"}


def test_tenants_cannot_read_each_others_secrets(vault, stores, tenants):
    from cortex_operator.connections import SecretNotFound

    a, b = tenants
    ref = vault.put(a, PLAIN)
    assert stores["secrets"].get(b, ref) is None
    with pytest.raises(SecretNotFound):
        vault.get(b, ref)
    assert stores["secrets"].list_for_tenant(b) == []
    assert stores["secrets"].delete(b, ref) is False
    assert vault.get(a, ref).reveal() == PLAIN


def test_a_ciphertext_copied_to_another_tenant_does_not_decrypt(vault, admin, tenants):
    from cortex_operator.connections import SecretUnreadable

    a, b = tenants
    ref = vault.put(a, PLAIN)
    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (a,))
    row = admin.execute("SELECT key_id, version, nonce, ciphertext, created_at, updated_at FROM vault_secret "
                        "WHERE ref = %s", (ref,)).fetchone()
    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (b,))
    admin.execute("INSERT INTO vault_secret VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (b, ref, *row))
    with pytest.raises(SecretUnreadable):
        vault.get(b, ref)


def test_replace_counts_versions_and_an_old_row_cannot_be_written_back(vault, stores, admin, tenants):
    import psycopg

    a, _ = tenants
    ref = vault.put(a, PLAIN)
    old = stores["secrets"].get(a, ref)
    assert old.version == 1
    vault.replace(a, ref, "newer-value")
    assert stores["secrets"].get(a, ref).version == 2
    assert stores["secrets"].put(old, replace=True) is False
    assert stores["secrets"].put(old, replace=False) is False
    assert vault.get(a, ref).reveal() == "newer-value"
    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (a,))
    with pytest.raises(psycopg.errors.RaiseException):  # the database refuses a version going backwards
        admin.execute("UPDATE vault_secret SET version = 1, nonce = %s, ciphertext = %s WHERE ref = %s",
                      (old.nonce, old.ciphertext, ref))
    with pytest.raises(psycopg.errors.RaiseException):
        admin.execute("UPDATE vault_secret SET version = 2 WHERE ref = %s", (ref,))


def test_editing_the_version_column_breaks_decryption(vault, admin, tenants):
    from cortex_operator.connections import SecretUnreadable

    a, _ = tenants
    ref = vault.put(a, PLAIN)
    admin.execute("SELECT set_config('app.tenant_id', %s, false)", (a,))
    admin.execute("ALTER TABLE vault_secret DISABLE TRIGGER vault_secret_guard")
    try:
        admin.execute("UPDATE vault_secret SET version = 2 WHERE ref = %s", (ref,))
    finally:
        admin.execute("ALTER TABLE vault_secret ENABLE TRIGGER vault_secret_guard")
    with pytest.raises(SecretUnreadable):
        vault.get(a, ref)


def test_rotation_over_postgres(stores, clock, tenants):
    from cortex_operator.connections import KeyRing, Vault

    a, b = tenants
    old, new = os.urandom(32), os.urandom(32)
    refs = {t: Vault(KeyRing(old), stores["secrets"], clock).put(t, f"{PLAIN}-{t[-4:]}") for t in (a, b)}
    rotated = Vault(KeyRing(new, [old]), stores["secrets"], clock)
    assert rotated.stale_count([a, b]) == 2
    assert rotated.rewrap_all([a, b]) == 2
    assert rotated.stale_count([a, b]) == 0
    only_new = Vault(KeyRing(new), stores["secrets"], clock)
    assert all(only_new.get(t, r).reveal() == f"{PLAIN}-{t[-4:]}" for t, r in refs.items())
    assert stores["secrets"].get(a, refs[a]).version == 2


def test_vault_store_without_a_tenant_list_refuses_to_guess(database, clock):
    from cortex_operator.connections import KeyRing, Vault, VaultError
    from cortex_operator.connections.pg import PostgresSecretStore

    with pytest.raises(VaultError):
        Vault(KeyRing(os.urandom(32)), PostgresSecretStore(database), clock).rewrap_all()


# ---------------------------------------------------------------- connections

def _registry(stores, clock, vault):
    from cortex_operator.connections import ConnectionRegistry, ProviderRegistry

    return ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                              connections=stores["connections"], vault=vault, health=stores["health"],
                              environ={}, mock_tenants=set())


def test_registry_on_postgres_resolves_and_isolates_tenants(stores, clock, vault, tenants):
    from cortex_operator.contracts.enums import Capability

    a, b = tenants
    reg = _registry(stores, clock, vault)
    assert len(reg.resolve(a, Capability.MESSAGING).ordered_connection_ids) == 1
    rows_a = stores["connections"].list_for_tenant(a)
    assert {r.provider for r in rows_a} == {"manual"} and stores["connections"].list_for_tenant(b) == []
    assert stores["connections"].get(b, rows_a[0].id) is None
    assert stores["connections"].delete(b, rows_a[0].id) is False


def test_secret_on_a_connection_lands_in_the_vault_table_only(stores, clock, vault, admin, tenants):
    from cortex_operator.contracts.enums import Capability, ConnectionMode
    from cortex_operator.contracts.testing.mocks import MockProvider

    a, _ = tenants
    reg = _registry(stores, clock, vault)
    reg.providers.register(Capability.TEXT, "hosted-x", ConnectionMode.HOSTED,
                          lambda ctx: MockProvider(Capability.TEXT, ctx.clock, "hosted-x"))
    row = reg.add(a, Capability.TEXT, "hosted-x", secret=PLAIN)
    assert vault.get(a, row.secret_ref).reveal() == PLAIN
    assert admin.execute("SELECT count(*) FROM connection WHERE settings::text LIKE %s OR secret_ref LIKE %s",
                         (f"%{PLAIN}%", f"%{PLAIN}%")).fetchone()[0] == 0
    reg.remove(a, row.id)
    assert not vault.exists(a, row.secret_ref)


def test_set_order_is_atomic_on_postgres(stores, clock, vault, tenants):
    from cortex_operator.connections import InvalidConnection, ManualRequired
    from cortex_operator.contracts.enums import Capability

    a, _ = tenants
    reg = _registry(stores, clock, vault)
    floor = reg.ensure_manual(a, Capability.TEXT)[0]
    before = stores["connections"].get(a, floor.id)
    with pytest.raises((ManualRequired, InvalidConnection)):
        reg.set_order(a, [floor.id])
    assert stores["connections"].get(a, floor.id) == before


# ---------------------------------------------------------------- health

def test_health_check_state_survives_a_restart_on_postgres(stores, clock, vault, tenants):
    from cortex_operator.connections import HealthMonitor, ListEventSink
    from cortex_operator.contracts.enums import Capability, Channel
    from cortex_operator.contracts.events import EventName
    from cortex_operator.contracts.ids import new_id
    from cortex_operator.contracts.models import ChannelAccount

    a, b = tenants
    reg = _registry(stores, clock, vault)
    reg.ensure_manual(a, Capability.TEXT)
    stores["accounts"].put(ChannelAccount(id=new_id(clock), created_at=clock.now(), tenant_id=a,
                                          channel=Channel.FACEBOOK, handle="@x",
                                          token_expires_at=clock.now() + timedelta(days=3)))
    sink = ListEventSink()
    HealthMonitor(clock=clock, registry=reg, accounts=stores["accounts"], state=stores["health"],
                  sink=sink).run_if_due()
    assert [e.name for e in sink.events] == [EventName.TOKEN_EXPIRING]
    assert sink.events[0].tenant_id == a
    # a new monitor (a worker restart) reads the same state: no repeat warning, and not due for a day
    again = ListEventSink()
    mon = HealthMonitor(clock=clock, registry=reg, accounts=stores["accounts"], state=stores["health"], sink=again)
    assert mon.run_if_due() is None
    clock.advance(days=1)
    mon.run_if_due()
    assert again.events == []
    assert stores["health"].last_run(b) is None


# ---------------------------------------------------------------- the schema itself

def test_new_tables_are_behind_row_level_security(admin):
    from cortex_operator.store import audit_rls

    assert audit_rls(admin) == []
    tables = {r[0] for r in admin.execute("SELECT relname FROM pg_class WHERE relrowsecurity").fetchall()}
    assert {"vault_secret", "connection_health", "token_warning", "health_run"} <= tables
