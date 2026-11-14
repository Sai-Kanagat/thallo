"""The global kill switch (migration 0013): readable by the app role, writable only by the owner,
and audited as a read-only table above tenants."""
from __future__ import annotations

import psycopg
import pytest

from thallo.store import audit_rls
from thallo.store.adapters import read_system_paused


def test_app_role_reads_the_switch(database, tenants):
    with database.tenant(tenants[0].id) as repo:
        assert read_system_paused(repo) is False


@pytest.mark.parametrize("statement", [
    "UPDATE system_setting SET system_paused = true",
    "INSERT INTO system_setting (id, system_paused) VALUES (true, true)",
    "DELETE FROM system_setting",
    "TRUNCATE system_setting",
])
def test_app_role_cannot_write_the_switch(database, tenants, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenants[0].id) as repo:
        repo.execute(statement)


def test_owner_flips_the_switch_and_a_missing_row_reads_as_paused(admin, database, tenants):
    try:
        admin.execute("UPDATE system_setting SET system_paused = true")
        with database.tenant(tenants[0].id) as repo:
            assert read_system_paused(repo) is True
        admin.execute("DELETE FROM system_setting")
        with database.tenant(tenants[0].id) as repo:
            assert read_system_paused(repo) is True  # fail closed
    finally:
        admin.execute("INSERT INTO system_setting (id, system_paused) VALUES (true, false) "
                      "ON CONFLICT (id) DO UPDATE SET system_paused = false")
    with database.tenant(tenants[0].id) as repo:
        assert read_system_paused(repo) is False


def test_audit_accepts_the_read_only_table(admin):
    assert audit_rls(admin) == []


def test_audit_catches_a_write_grant_or_a_write_policy(admin):
    with admin.transaction(force_rollback=True):
        admin.execute("GRANT UPDATE ON system_setting TO cortex_app")
        admin.execute("CREATE POLICY widened ON system_setting FOR ALL TO cortex_app USING (true) WITH CHECK (true)")
        assert audit_rls(admin) == [
            "system_setting: policy widened for cortex_app allows more than SELECT",
            "system_setting: cortex_app holds UPDATE; only SELECT is allowed",
        ]
    with admin.transaction(force_rollback=True):
        admin.execute("ALTER TABLE system_setting DISABLE ROW LEVEL SECURITY")
        assert audit_rls(admin) == ["system_setting: row-level security not enabled"]
    assert audit_rls(admin) == []
