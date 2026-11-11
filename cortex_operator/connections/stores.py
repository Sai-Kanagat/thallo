"""Storage seams. Each store is a Protocol with an in-memory implementation.

WP-01's `app_enable_tenant_rls` was not in this worktree, so no migration ships with WP-02. The
Postgres implementations of these Protocols are a small follow-up once WP-01 lands.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from cortex_operator.contracts.events import Event
from cortex_operator.contracts.models import ChannelAccount, Connection


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
    def get(self, tenant_id: str, ref: str) -> SealedSecret | None: ...
    def put(self, sealed: SealedSecret, *, replace: bool) -> bool: ...
    def delete(self, tenant_id: str, ref: str) -> bool: ...
    def all(self) -> Iterable[SealedSecret]: ...


class InMemorySecretStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], SealedSecret] = {}

    def get(self, tenant_id: str, ref: str) -> SealedSecret | None:
        return self.rows.get((tenant_id, ref))

    def put(self, sealed: SealedSecret, *, replace: bool) -> bool:
        key = (sealed.tenant_id, sealed.ref)
        if key in self.rows and not replace:
            return False
        self.rows[key] = sealed
        return True

    def delete(self, tenant_id: str, ref: str) -> bool:
        return self.rows.pop((tenant_id, ref), None) is not None

    def all(self) -> list[SealedSecret]:
        return list(self.rows.values())


class ConnectionStore(Protocol):
    def list_for_tenant(self, tenant_id: str) -> list[Connection]: ...
    def list_all(self) -> list[Connection]: ...
    def get(self, tenant_id: str, connection_id: str) -> Connection | None: ...
    def put(self, connection: Connection) -> None: ...
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
    def last_connected(self, connection_id: str) -> bool | None: ...
    def set_connected(self, connection_id: str, connected: bool) -> None: ...
    def warned_expiry(self, account_id: str) -> datetime | None: ...
    def set_warned_expiry(self, account_id: str, expires_at: datetime) -> None: ...
    def last_run(self) -> datetime | None: ...
    def set_last_run(self, at: datetime) -> None: ...


class InMemoryHealthState:
    def __init__(self) -> None:
        self._connected: dict[str, bool] = {}
        self._warned: dict[str, datetime] = {}
        self._last_run: datetime | None = None

    def last_connected(self, connection_id: str) -> bool | None:
        return self._connected.get(connection_id)

    def set_connected(self, connection_id: str, connected: bool) -> None:
        self._connected[connection_id] = connected

    def warned_expiry(self, account_id: str) -> datetime | None:
        return self._warned.get(account_id)

    def set_warned_expiry(self, account_id: str, expires_at: datetime) -> None:
        self._warned[account_id] = expires_at

    def last_run(self) -> datetime | None:
        return self._last_run

    def set_last_run(self, at: datetime) -> None:
        self._last_run = at


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class ListEventSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
