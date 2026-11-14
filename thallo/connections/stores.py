"""Storage seams. Each store is a Protocol with an in-memory implementation.

WP-01's `app_enable_tenant_rls` was not in this worktree, so no migration ships with WP-02. The
Postgres implementations of these Protocols are a small follow-up once WP-01 lands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from thallo.contracts.events import Event
from thallo.contracts.models import ChannelAccount, Connection


@dataclass(frozen=True)
class SealedSecret:
    tenant_id: str
    ref: str
    key_id: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)
    created_at: datetime
    updated_at: datetime
    version: int = 1


class SecretStore(Protocol):
    """Sealed secrets. `put` is compare-and-set on `version`: a new row must be version 1, a
    replacement must be exactly the stored version plus one, so an old row cannot be written back.
    It returns False when that does not hold."""

    def get(self, tenant_id: str, ref: str) -> SealedSecret | None: ...
    def put(self, sealed: SealedSecret, *, replace: bool) -> bool: ...
    def delete(self, tenant_id: str, ref: str) -> bool: ...
    def list_for_tenant(self, tenant_id: str) -> list[SealedSecret]: ...
    def tenant_ids(self) -> list[str]: ...


class InMemorySecretStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], SealedSecret] = {}

    def get(self, tenant_id: str, ref: str) -> SealedSecret | None:
        return self.rows.get((tenant_id, ref))

    def put(self, sealed: SealedSecret, *, replace: bool) -> bool:
        key = (sealed.tenant_id, sealed.ref)
        old = self.rows.get(key)
        if replace:
            if old is None or sealed.version != old.version + 1:
                return False
        elif old is not None or sealed.version != 1:
            return False
        self.rows[key] = sealed
        return True

    def delete(self, tenant_id: str, ref: str) -> bool:
        return self.rows.pop((tenant_id, ref), None) is not None

    def list_for_tenant(self, tenant_id: str) -> list[SealedSecret]:
        return [r for (t, _), r in self.rows.items() if t == tenant_id]

    def tenant_ids(self) -> list[str]:
        return sorted({t for t, _ in self.rows})


class ConnectionStore(Protocol):
    def list_for_tenant(self, tenant_id: str) -> list[Connection]: ...
    def list_all(self) -> list[Connection]: ...
    def get(self, tenant_id: str, connection_id: str) -> Connection | None: ...
    def put(self, connection: Connection) -> None: ...
    def put_many(self, connections: list[Connection]) -> None: ...  # all or nothing
    def delete(self, tenant_id: str, connection_id: str) -> bool: ...


class InMemoryConnectionStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Connection] = {}

    def list_for_tenant(self, tenant_id: str) -> list[Connection]:
        return [c for (t, _), c in self.rows.items() if t == tenant_id]

    def list_all(self) -> list[Connection]:
        return list(self.rows.values())

    def get(self, tenant_id: str, connection_id: str) -> Connection | None:
        return self.rows.get((tenant_id, connection_id))

    def put(self, connection: Connection) -> None:
        self.rows[(connection.tenant_id, connection.id)] = connection

    def put_many(self, connections: list[Connection]) -> None:
        staged = {(c.tenant_id, c.id): c for c in connections}
        self.rows.update(staged)

    def delete(self, tenant_id: str, connection_id: str) -> bool:
        return self.rows.pop((tenant_id, connection_id), None) is not None


class ChannelAccountStore(Protocol):
    def list_all(self) -> list[ChannelAccount]: ...


class InMemoryChannelAccountStore:
    def __init__(self) -> None:
        self.rows: dict[str, ChannelAccount] = {}

    def put(self, account: ChannelAccount) -> None:
        self.rows[account.id] = account

    def list_all(self) -> list[ChannelAccount]:
        return list(self.rows.values())


class HealthState(Protocol):
    """What the daily check remembers, per tenant."""

    def last_connected(self, tenant_id: str, connection_id: str) -> bool | None: ...
    def set_connected(self, tenant_id: str, connection_id: str, connected: bool) -> None: ...
    def warned_expiry(self, tenant_id: str, account_id: str) -> datetime | None: ...
    def set_warned_expiry(self, tenant_id: str, account_id: str, expires_at: datetime) -> None: ...
    def last_run(self, tenant_id: str) -> datetime | None: ...
    def set_last_run(self, tenant_id: str, at: datetime) -> None: ...


class InMemoryHealthState:
    def __init__(self) -> None:
        self._connected: dict[tuple[str, str], bool] = {}
        self._warned: dict[tuple[str, str], datetime] = {}
        self._last_run: dict[str, datetime] = {}

    def last_connected(self, tenant_id: str, connection_id: str) -> bool | None:
        return self._connected.get((tenant_id, connection_id))

    def set_connected(self, tenant_id: str, connection_id: str, connected: bool) -> None:
        self._connected[(tenant_id, connection_id)] = connected

    def warned_expiry(self, tenant_id: str, account_id: str) -> datetime | None:
        return self._warned.get((tenant_id, account_id))

    def set_warned_expiry(self, tenant_id: str, account_id: str, expires_at: datetime) -> None:
        self._warned[(tenant_id, account_id)] = expires_at

    def last_run(self, tenant_id: str) -> datetime | None:
        return self._last_run.get(tenant_id)

    def set_last_run(self, tenant_id: str, at: datetime) -> None:
        self._last_run[tenant_id] = at


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class ListEventSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
