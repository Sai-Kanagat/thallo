"""Row-level security, proven by connecting as the application role (cortex_app).

The admin connection is a superuser and sees everything; it is used to set up and to check
what really sits in the tables. Every claim about isolation is made through the app role.
"""
from __future__ import annotations

import psycopg
import pytest

from cortex_operator.contracts.enums import ActorKind, ClaimKind
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import TABLES, AuditEntry, Claim, Lead, Person
from cortex_operator.store import InvalidTenant, audit_rls

NO_LINKS = {"first_touch_id": None, "last_touch_id": None, "owner_id": None}


def person(repo, name):
    return repo.insert(Person(id=repo.new_id(), tenant_id=repo.tenant_id, created_at=repo.now(),
                              display_name=name, account_id=None))


def seeded(database, tenants):
    a, b = tenants
    with database.tenant(a.id) as repo:
        pa = person(repo, "alice")
        repo.insert(Lead(id=repo.new_id(), tenant_id=a.id, created_at=repo.now(), person_id=pa.id, **NO_LINKS))
    with database.tenant(b.id) as repo:
        pb = person(repo, "bob")
        repo.insert(Lead(id=repo.new_id(), tenant_id=b.id, created_at=repo.now(), person_id=pb.id, **NO_LINKS))
    return a, b, pa, pb


def test_app_role_is_not_privileged(admin):
    sup, bypass = admin.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='cortex_app'").fetchone()
    assert (sup, bypass) == (False, False)


def test_tenant_a_sees_nothing_of_tenant_b(database, tenants, admin):
    a, b, pa, pb = seeded(database, tenants)
    assert admin.execute("SELECT count(*) FROM person WHERE tenant_id IN (%s,%s)", (a.id, b.id)).fetchone()[0] == 2
    with database.tenant(a.id) as repo:
        assert [p.display_name for p in repo.list(Person)] == ["alice"]
        assert repo.get(Person, pb.id) is None  # by id, even knowing it
        assert repo.execute("SELECT count(*) FROM person WHERE tenant_id = %s", (b.id,)).fetchone()[0] == 0
        assert repo.execute("SELECT count(*) FROM lead").fetchone()[0] == 1
        assert repo.execute("SELECT name FROM tenant").fetchall() == [("Alpha",)]
    with database.tenant(b.id) as repo:
        assert [p.display_name for p in repo.list(Person)] == ["bob"]


def test_no_tenant_set_means_no_rows(urls, database, tenants):
    seeded(database, tenants)
    with psycopg.connect(urls["app"]) as conn:
        for table in ("person", "lead", "tenant", "engine_draft"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        conn.execute("SELECT set_config('app.tenant_id', '', true)")
        assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == 0


def test_cannot_insert_for_another_tenant(database, tenants):
    a, b, _, _ = seeded(database, tenants)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(a.id) as repo:
        person_row = Person(id=new_id(repo.clock), tenant_id=b.id, created_at=repo.now(),
                            display_name="mallory", account_id=None)
        repo.insert(person_row)  # RLS WITH CHECK refuses
    with database.tenant(b.id) as repo:
        assert [p.display_name for p in repo.list(Person)] == ["bob"]


def test_cannot_update_or_delete_another_tenants_row(database, tenants, admin):
    a, b, _, pb = seeded(database, tenants)
    with database.tenant(a.id) as repo:
        assert repo.update(Person, pb.id, display_name="hacked") is False
        assert repo.delete(Person, pb.id) is False
    assert admin.execute("SELECT display_name FROM person WHERE id=%s", (pb.id,)).fetchone() == ("bob",)


def test_cannot_move_a_row_to_another_tenant(database, tenants, admin):
    a, b, pa, _ = seeded(database, tenants)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(a.id) as repo:
        repo.execute("UPDATE person SET tenant_id = %s WHERE id = %s", (b.id, pa.id))
    assert str(admin.execute("SELECT tenant_id FROM person WHERE id=%s", (pa.id,)).fetchone()[0]) == a.id


def test_composite_foreign_keys_still_hold_under_rls(database, tenants):
    a, b, pa, pb = seeded(database, tenants)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), database.tenant(a.id) as repo:
        repo.insert(Lead(id=repo.new_id(), tenant_id=a.id, created_at=repo.now(), person_id=pb.id, **NO_LINKS))


def test_tenant_setting_does_not_outlive_the_transaction(urls, database, tenants):
    a, b, _, _ = seeded(database, tenants)
    with database.tenant(a.id) as repo:
        assert repo.execute("SELECT current_setting('app.tenant_id')").fetchone()[0] == a.id
    with psycopg.connect(urls["app"]) as conn:
        assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == 0


def test_app_role_cannot_create_or_edit_tenants(database, tenants):
    a, _ = tenants
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(a.id) as repo:
        repo.execute("UPDATE tenant SET name = 'x' WHERE id = %s", (a.id,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(a.id) as repo:
        repo.execute("DELETE FROM tenant WHERE id = %s", (a.id,))
    with database.tenant(a.id) as repo:  # the kill switch is allowed
        repo.execute("UPDATE tenant SET paused = true WHERE id = %s", (a.id,))
        repo.execute("UPDATE tenant SET paused = false WHERE id = %s", (a.id,))


def test_ledgers_are_append_only_for_the_app_role(database, tenants):
    a, _ = tenants
    with database.tenant(a.id) as repo:
        repo.insert(AuditEntry(id=repo.new_id(), tenant_id=a.id, created_at=repo.now(), seq=1,
                               actor_kind=ActorKind.SYSTEM, actor_id="t", action="x", entry_hash="h",
                               occurred_at=repo.now()))
        assert repo.count(AuditEntry) == 1
    for statement in ("UPDATE audit_log SET action = 'y'", "DELETE FROM audit_log"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(a.id) as repo:
            repo.execute(statement)


def test_typed_round_trip(database, tenants):
    a, _ = tenants
    with database.tenant(a.id) as repo:
        claim = repo.insert(Claim(id=repo.new_id(), tenant_id=a.id, created_at=repo.now(), code="C-0001",
                                  kind=ClaimKind.PRICE, source_id=None, verified_by=None, value="499", unit="EUR",
                                  languages={"en": "from 499 EUR", "it": "da 499 EUR"}))
        assert repo.get(Claim, claim.id) == claim
        assert repo.list(Claim, code="C-0001") == [claim]
        assert repo.update(Claim, claim.id, languages={"en": "x"}) is True
        assert repo.get(Claim, claim.id).languages == {"en": "x"}
        with pytest.raises(KeyError):
            repo.list(Claim, nope=1)
        with pytest.raises(ValueError, match="tenant_id"):
            repo.update(Claim, claim.id, tenant_id=a.id)


def test_bad_tenant_id_is_refused(database):
    for bad in ("", "x", "1; DROP TABLE tenant", None):
        with pytest.raises(InvalidTenant), database.tenant(bad):  # type: ignore[arg-type]
            pass


def test_privileged_url_is_refused(urls, clock):
    from cortex_operator.store import Database

    with pytest.raises(InvalidTenant), Database(urls["admin"], clock).tenant("0190f3a0-0000-7000-8000-000000000001"):
        pass


def test_every_table_has_a_full_policy(admin):
    assert audit_rls(admin) == []
    covered = {r[0] for r in admin.execute("SELECT tablename FROM pg_policies WHERE policyname='tenant_isolation'")}
    assert {t.__table__ for t in TABLES} <= covered  # all 50 core tables
    assert {"engine_passport", "engine_taste", "engine_sealed"} <= covered


def test_audit_fails_for_a_table_without_a_policy(admin):
    """The guard works: a future table added without app_enable_tenant_rls is caught."""
    with admin.transaction(force_rollback=True):
        admin.execute("CREATE TABLE zz_future (id uuid PRIMARY KEY, tenant_id uuid NOT NULL)")
        assert audit_rls(admin) == ["zz_future: row-level security not enabled",
                                    "zz_future: row-level security not forced (owner would bypass it)",
                                    "zz_future: no policy for all commands"]
        admin.execute("SELECT app_enable_tenant_rls('zz_future')")
        assert audit_rls(admin) == []
    assert audit_rls(admin) == []


def test_audit_fails_for_a_weak_policy(admin):
    with admin.transaction(force_rollback=True):
        admin.execute("DROP POLICY tenant_isolation ON person")
        admin.execute("CREATE POLICY tenant_isolation ON person USING (true) WITH CHECK (true)")
        assert audit_rls(admin) == ["person: policy does not filter and check on app_current_tenant()"]
