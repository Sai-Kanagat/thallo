"""Tenant-scoped Postgres access.

`Database.tenant(tenant_id)` is the only way application code gets a connection. It opens
one transaction, sets `app.tenant_id` for that transaction only (the equivalent of
SET LOCAL), and yields a `TenantRepo`. Row-level security then limits every statement to
that tenant, whatever SQL runs. When the block ends the transaction commits (or rolls back
on an error) and the setting disappears with it, so a pooled connection cannot leak it.

The URL must connect as the `cortex_app` role. A superuser or BYPASSRLS role would skip
the policies, so `Database.tenant` refuses to hand out a connection for such a role.

Time and ids come from the Clock given at construction. Nothing here reads the system clock.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import Operator, Row, Tenant

R = TypeVar("R", bound=Row)


class InvalidTenant(ValueError):
    """The tenant id is not a UUID, or the connection role would bypass row-level security."""


def _check_tenant(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not _is_any_uuid(tenant_id):
        raise InvalidTenant(f"not a tenant id: {tenant_id!r}")
    return tenant_id.lower()


def _is_any_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def _scope(conn: psycopg.Connection, tenant_id: str) -> None:
    # set_config(..., true) is SET LOCAL with a bound parameter: no string building.
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
    # Times are UTC (contracts), whatever the server, database or role default time zone is.
    conn.execute("SELECT set_config('TimeZone', 'UTC', true)")


def _column(model: type[Row], name: str) -> sql.Identifier:
    if name not in model.model_fields:
        raise KeyError(f"{model.__table__} has no column {name!r}")
    return sql.Identifier(name)


def _to_db(model: type[Row], row: Row) -> dict[str, Any]:
    as_json = json.loads(row.model_dump_json())
    out: dict[str, Any] = {}
    for name in model.model_fields:
        value = getattr(row, name)
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, dict | list | tuple):
            value = Jsonb(as_json[name])
        out[name] = value
    return out


def _value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict | list):
        return Jsonb(value)
    return value


def _from_db(model: type[R], record: dict[str, Any]) -> R:
    return model.model_validate({k: str(v) if isinstance(v, UUID) else v for k, v in record.items()})


class TenantRepo:
    """Typed reads and writes for one tenant, inside one transaction."""

    def __init__(self, conn: psycopg.Connection, tenant_id: str, clock: Clock) -> None:
        self.conn = conn
        self.tenant_id = tenant_id
        self.clock = clock

    def now(self) -> datetime:
        return self.clock.now()

    def new_id(self) -> str:
        return new_id(self.clock)

    def execute(self, query: str | sql.Composable, params: Any = None) -> psycopg.Cursor:
        """Raw SQL, for trusted code only. Row-level security still applies, but prefer the typed methods."""
        return self.conn.execute(query, params)

    def insert(self, row: R) -> R:
        model = type(row)
        data = _to_db(model, row)
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in data)
        marks = sql.SQL(", ").join(sql.Placeholder() for _ in data)
        self.conn.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(model.__table__), cols, marks),
            list(data.values()))
        return row

    def get(self, model: type[R], row_id: str) -> R | None:
        cur = self.conn.cursor(row_factory=dict_row)
        cur.execute(sql.SQL("SELECT * FROM {} WHERE id = %s").format(sql.Identifier(model.__table__)), (row_id,))
        record = cur.fetchone()
        return _from_db(model, record) if record else None

    def list(self, model: type[R], *, limit: int = 1000, **where: Any) -> list[R]:
        """Rows of this tenant, oldest first (ids are time-ordered), filtered by equality."""
        clauses = [sql.SQL("{} = %s").format(_column(model, k)) for k in where]
        query = sql.SQL("SELECT * FROM {}").format(sql.Identifier(model.__table__))
        if clauses:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
        query += sql.SQL(" ORDER BY id LIMIT %s")
        cur = self.conn.cursor(row_factory=dict_row)
        cur.execute(query, [_value(v) for v in where.values()] + [limit])
        return [_from_db(model, r) for r in cur.fetchall()]

    def update(self, model: type[R], row_id: str, **fields: Any) -> bool:
        """Set columns on one row. False when no row of this tenant has that id."""
        if not fields:
            raise ValueError("nothing to update")
        for protected in ("id", "tenant_id", "created_at"):
            if protected in fields:
                raise ValueError(f"{protected} cannot be updated")
        sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(_column(model, k)) for k in fields)
        cur = self.conn.execute(
            sql.SQL("UPDATE {} SET {} WHERE id = %s").format(sql.Identifier(model.__table__), sets),
            [_value(v) for v in fields.values()] + [row_id])
        return cur.rowcount == 1

    def delete(self, model: type[R], row_id: str) -> bool:
        cur = self.conn.execute(
            sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(model.__table__)), (row_id,))
        return cur.rowcount == 1

    def count(self, model: type[R]) -> int:
        cur = self.conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(model.__table__)))
        return cur.fetchone()[0]  # type: ignore[index]


class Database:
    """Connection source. `url` connects as cortex_app. `admin_url` (optional) is the owner,
    used only to provision tenants."""

    def __init__(self, url: str, clock: Clock, admin_url: str | None = None) -> None:
        self.url = url
        self.clock = clock
        self.admin_url = admin_url

    @contextmanager
    def tenant(self, tenant_id: str) -> Iterator[TenantRepo]:
        tenant_id = _check_tenant(tenant_id)
        with psycopg.connect(self.url) as conn:  # commits on success, rolls back on error
            privileged = conn.execute(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
            if privileged is None or privileged[0]:
                raise InvalidTenant("the application URL must connect as a role without BYPASSRLS")
            _scope(conn, tenant_id)
            yield TenantRepo(conn, tenant_id, self.clock)

    def create_operator(self, operator: Operator) -> Operator:
        """Provision an operator (the layer above tenants). Needs admin_url."""
        if self.admin_url is None:
            raise InvalidTenant("create_operator needs admin_url (the owner role)")
        with psycopg.connect(self.admin_url) as conn:
            conn.execute("INSERT INTO operator (id, created_at, name) VALUES (%s, %s, %s)",
                         (operator.id, operator.created_at, operator.name))
        return operator

    def create_tenant(self, tenant: Tenant) -> Tenant:
        """Provision a tenant row. Needs admin_url: the app role cannot create tenants."""
        if self.admin_url is None:
            raise InvalidTenant("create_tenant needs admin_url (the owner role)")
        _check_tenant(tenant.id)
        with psycopg.connect(self.admin_url) as conn:
            _scope(conn, tenant.id)  # the policy applies to the owner too (FORCE), unless it bypasses
            repo = TenantRepo(conn, tenant.id, self.clock)
            repo.insert(tenant)
        return tenant
