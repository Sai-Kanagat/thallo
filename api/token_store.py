"""API tokens at rest. Owner-side only: `api_token` (migration 0035) has no grant to the app role.

A token is `thl_` plus 43 random url-safe characters. The store keeps its SHA-256 and a short
display prefix, never the token. The token is shown once, when it is made.

Every request resolves the token afresh (no cache), so a revocation takes effect on the next call.
Expiry and revocation are checked by the caller against its Clock.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

TOKEN_PREFIX = "thl_"


class TokenScope(StrEnum):
    READ = "read"
    DRAFT = "draft"


@dataclass(frozen=True)
class ApiToken:
    id: str
    created_at: datetime
    tenant_id: str
    app_user_id: str
    name: str
    token_sha256: str
    prefix: str
    scopes: frozenset[TokenScope]
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    last_used_at: datetime | None = None

    def usable(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_token(value: str) -> bool:
    return value.startswith(TOKEN_PREFIX) and 20 <= len(value) <= 100 and value.isascii()


class TokenStore(Protocol):
    def add(self, token: ApiToken) -> None: ...
    def by_hash(self, token_sha256: str) -> ApiToken | None: ...
    def list(self, tenant_id: str) -> list[ApiToken]: ...
    def revoke(self, tenant_id: str, token_id: str, by: str, at: datetime) -> bool: ...
    def touch(self, token_id: str, at: datetime) -> None: ...


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._rows: dict[str, ApiToken] = {}
        self._lock = threading.Lock()

    def add(self, token: ApiToken) -> None:
        with self._lock:
            if token.id in self._rows or any(t.token_sha256 == token.token_sha256 for t in self._rows.values()):
                raise ValueError("duplicate token")
            self._rows[token.id] = token

    def by_hash(self, token_sha256: str) -> ApiToken | None:
        with self._lock:
            return next((t for t in self._rows.values() if t.token_sha256 == token_sha256), None)

    def list(self, tenant_id: str) -> list[ApiToken]:
        with self._lock:
            return sorted((t for t in self._rows.values() if t.tenant_id == tenant_id), key=lambda t: t.created_at)

    def revoke(self, tenant_id: str, token_id: str, by: str, at: datetime) -> bool:
        with self._lock:
            t = self._rows.get(token_id)
            if t is None or t.tenant_id != tenant_id or t.revoked_at is not None:
                return False
            self._rows[token_id] = replace(t, revoked_at=at, revoked_by=by)
            return True

    def touch(self, token_id: str, at: datetime) -> None:
        with self._lock:
            t = self._rows.get(token_id)
            if t is not None:
                self._rows[token_id] = replace(t, last_used_at=at)


_COLS = ("id, created_at, tenant_id, app_user_id, name, token_sha256, prefix, scopes, expires_at, revoked_at, "
         "revoked_by, last_used_at")


def _row(r: tuple) -> ApiToken:
    return ApiToken(id=str(r[0]), created_at=r[1], tenant_id=str(r[2]), app_user_id=str(r[3]), name=r[4],
                    token_sha256=r[5], prefix=r[6], scopes=frozenset(TokenScope(s) for s in r[7]),
                    expires_at=r[8], revoked_at=r[9], revoked_by=str(r[10]) if r[10] else None, last_used_at=r[11])


class OwnerTokenStore:
    """Connects with the owner URL for every call. `api_token` is not forced, so the owner reads
    and writes it on Supabase as on Docker; the app role cannot."""

    def __init__(self, owner_url: str) -> None:
        self.owner_url = owner_url

    def _conn(self):  # noqa: ANN202
        import psycopg

        return psycopg.connect(self.owner_url)

    def add(self, token: ApiToken) -> None:
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO api_token ({_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (token.id, token.created_at, token.tenant_id, token.app_user_id, token.name, token.token_sha256,
                 token.prefix, sorted(s.value for s in token.scopes), token.expires_at, token.revoked_at,
                 token.revoked_by, token.last_used_at))

    def by_hash(self, token_sha256: str) -> ApiToken | None:
        with self._conn() as conn:
            r = conn.execute(f"SELECT {_COLS} FROM api_token WHERE token_sha256 = %s", (token_sha256,)).fetchone()
        return _row(r) if r else None

    def list(self, tenant_id: str) -> list[ApiToken]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {_COLS} FROM api_token WHERE tenant_id = %s ORDER BY created_at, id",
                                (tenant_id,)).fetchall()
        return [_row(r) for r in rows]

    def revoke(self, tenant_id: str, token_id: str, by: str, at: datetime) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE api_token SET revoked_at = %s, revoked_by = %s "
                               "WHERE id = %s AND tenant_id = %s AND revoked_at IS NULL", (at, by, token_id, tenant_id))
            return cur.rowcount == 1

    def touch(self, token_id: str, at: datetime) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE api_token SET last_used_at = %s WHERE id = %s", (at, token_id))


__all__ = ["TOKEN_PREFIX", "ApiToken", "InMemoryTokenStore", "OwnerTokenStore", "TokenScope", "TokenStore",
           "hash_token", "looks_like_token", "new_token"]
