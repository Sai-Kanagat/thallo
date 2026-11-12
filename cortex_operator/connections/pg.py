"""Postgres implementations of the connection stores (needs the `db` extra).

Every call runs in `Database.tenant(...)`: one transaction, the tenant set, row-level security
enforced for the application role. Nothing here can read another tenant's row.

Stores that list across tenants (rotation, the daily health check) take `tenant_ids`, a callable
that returns the tenants to cover. Row-level security hides every other tenant from the app role,
so the worker supplies that list from its own provisioning data.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from cortex_operator.connections.errors import VaultError
from cortex_operator.connections.stores import SealedSecret
from cortex_operator.contracts.models import ChannelAccount, Connection
from cortex_operator.store import Database

TenantIds = Callable[[], Iterable[str]]

_SECRET_COLS = "tenant_id, ref, key_id, version, nonce, ciphertext, created_at, updated_at"


def _sealed(r: tuple) -> SealedSecret:
    return SealedSecret(tenant_id=str(r[0]), ref=r[1], key_id=r[2], version=r[3], nonce=bytes(r[4]),
                        ciphertext=bytes(r[5]), created_at=r[6], updated_at=r[7])


class _CrossTenant:
    def __init__(self, db: Database, tenant_ids: TenantIds | None = None) -> None:
        self.db = db
        self._tenant_ids = tenant_ids

    def tenant_ids(self) -> list[str]:
        if self._tenant_ids is None:
            raise VaultError("this store cannot list tenants under row-level security; pass tenant_ids")
        return sorted(set(self._tenant_ids()))


class PostgresSecretStore(_CrossTenant):
    def get(self, tenant_id: str, ref: str) -> SealedSecret | None:
        with self.db.tenant(tenant_id) as repo:
            row = repo.execute(f"SELECT {_SECRET_COLS} FROM vault_secret WHERE tenant_id = %s AND ref = %s",
                               (tenant_id, ref)).fetchone()
        return _sealed(row) if row else None

    def put(self, sealed: SealedSecret, *, replace: bool) -> bool:
        with self.db.tenant(sealed.tenant_id) as repo:
            if not replace:
                if sealed.version != 1:
                    return False
                cur = repo.execute(
                    f"INSERT INTO vault_secret ({_SECRET_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (sealed.tenant_id, sealed.ref, sealed.key_id, sealed.version, sealed.nonce,
                     sealed.ciphertext, sealed.created_at, sealed.updated_at))
                return cur.rowcount == 1
            cur = repo.execute(
                "UPDATE vault_secret SET key_id = %s, version = %s, nonce = %s, ciphertext = %s, updated_at = %s "
                "WHERE tenant_id = %s AND ref = %s AND version = %s",
                (sealed.key_id, sealed.version, sealed.nonce, sealed.ciphertext, sealed.updated_at,
                 sealed.tenant_id, sealed.ref, sealed.version - 1))
            return cur.rowcount == 1

    def delete(self, tenant_id: str, ref: str) -> bool:
        with self.db.tenant(tenant_id) as repo:
            return repo.execute("DELETE FROM vault_secret WHERE tenant_id = %s AND ref = %s",
                                (tenant_id, ref)).rowcount == 1

    def list_for_tenant(self, tenant_id: str) -> list[SealedSecret]:
        with self.db.tenant(tenant_id) as repo:
            rows = repo.execute(f"SELECT {_SECRET_COLS} FROM vault_secret WHERE tenant_id = %s ORDER BY ref",
                                (tenant_id,)).fetchall()
        return [_sealed(r) for r in rows]


_MUTABLE = ("capability", "provider", "mode", "model", "role", "scope", "scope_ref", "priority", "enabled",
            "settings", "secret_ref")


def _upsert(repo, connection: Connection) -> None:  # noqa: ANN001
    if repo.get(Connection, connection.id) is None:
        repo.insert(connection)
    else:
        repo.update(Connection, connection.id, **{f: getattr(connection, f) for f in _MUTABLE})


class PostgresConnectionStore(_CrossTenant):
    def list_for_tenant(self, tenant_id: str) -> list[Connection]:
        with self.db.tenant(tenant_id) as repo:
            return repo.list(Connection, limit=100_000)

    def list_all(self) -> list[Connection]:
        return [c for t in self.tenant_ids() for c in self.list_for_tenant(t)]

    def get(self, tenant_id: str, connection_id: str) -> Connection | None:
        with self.db.tenant(tenant_id) as repo:
            return repo.get(Connection, connection_id)

    def put(self, connection: Connection) -> None:
        with self.db.tenant(connection.tenant_id) as repo:
            _upsert(repo, connection)

    def put_many(self, connections: list[Connection]) -> None:
        """One transaction, so all rows change or none do. Rows must belong to one tenant."""
        if not connections:
            return
        tenants = {c.tenant_id for c in connections}
        if len(tenants) != 1:
            raise ValueError("put_many takes rows of one tenant")
        with self.db.tenant(next(iter(tenants))) as repo:
            for c in connections:
                _upsert(repo, c)

    def delete(self, tenant_id: str, connection_id: str) -> bool:
        with self.db.tenant(tenant_id) as repo:
            return repo.delete(Connection, connection_id)


class PostgresChannelAccountStore(_CrossTenant):
    def put(self, account: ChannelAccount) -> None:
        with self.db.tenant(account.tenant_id) as repo:
            if repo.get(ChannelAccount, account.id) is None:
                repo.insert(account)
            else:
                repo.update(ChannelAccount, account.id, channel=account.channel, connection_id=account.connection_id,
                            handle=account.handle, token_expires_at=account.token_expires_at,
                            secret_ref=account.secret_ref)

    def list_all(self) -> list[ChannelAccount]:
        out: list[ChannelAccount] = []
        for t in self.tenant_ids():
            with self.db.tenant(t) as repo:
                out.extend(repo.list(ChannelAccount, limit=100_000))
        return out


class PostgresHealthState:
    def __init__(self, db: Database) -> None:
        self.db = db

    def last_connected(self, tenant_id: str, connection_id: str) -> bool | None:
        with self.db.tenant(tenant_id) as repo:
            row = repo.execute("SELECT connected FROM connection_health WHERE tenant_id = %s AND connection_id = %s",
                               (tenant_id, connection_id)).fetchone()
        return None if row is None else row[0]

    def set_connected(self, tenant_id: str, connection_id: str, connected: bool) -> None:
        with self.db.tenant(tenant_id) as repo:
            repo.execute(
                "INSERT INTO connection_health (tenant_id, connection_id, connected, checked_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, connection_id) "
                "DO UPDATE SET connected = EXCLUDED.connected, checked_at = EXCLUDED.checked_at",
                (tenant_id, connection_id, connected, repo.now()))

    def warned_expiry(self, tenant_id: str, account_id: str) -> datetime | None:
        with self.db.tenant(tenant_id) as repo:
            row = repo.execute("SELECT expires_at FROM token_warning WHERE tenant_id = %s "
                               "AND channel_account_id = %s", (tenant_id, account_id)).fetchone()
        return None if row is None else row[0]

    def set_warned_expiry(self, tenant_id: str, account_id: str, expires_at: datetime) -> None:
        with self.db.tenant(tenant_id) as repo:
            repo.execute(
                "INSERT INTO token_warning (tenant_id, channel_account_id, expires_at, warned_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, channel_account_id) "
                "DO UPDATE SET expires_at = EXCLUDED.expires_at, warned_at = EXCLUDED.warned_at",
                (tenant_id, account_id, expires_at, repo.now()))

    def last_run(self, tenant_id: str) -> datetime | None:
        with self.db.tenant(tenant_id) as repo:
            row = repo.execute("SELECT last_run FROM health_run WHERE tenant_id = %s", (tenant_id,)).fetchone()
        return None if row is None else row[0]

    def set_last_run(self, tenant_id: str, at: datetime) -> None:
        with self.db.tenant(tenant_id) as repo:
            repo.execute("INSERT INTO health_run (tenant_id, last_run) VALUES (%s, %s) "
                         "ON CONFLICT (tenant_id) DO UPDATE SET last_run = EXCLUDED.last_run", (tenant_id, at))
