"""Server-side sessions, one-time login codes, rate limits and the real `SessionVerifier`.

Sessions
- The browser holds only an opaque random token in an HttpOnly, Secure, SameSite=Strict cookie.
  The store keeps a hash of it, never the token.
- A session has an internal id (`Session.id`) that is never sent to the client. It is what a
  `HumanActor.session_id` names, so the verifier can find the session behind an actor.
- Every session starts anonymous (it carries the CSRF token for the login form). A verified code
  rotates it: new token, new id, new CSRF token. A tenant switch rotates it again.
- Idle timeout (default 15 minutes) and an absolute lifetime (default 12 hours), both on the Clock.

Only an INTERACTIVE session that finished the code flow and is bound to a membership counts as a
live human session. API tokens, jobs and services get other kinds and never qualify.

Everything here is in process memory: a restart signs everyone out, and two API processes do not
share sessions. A durable store needs a table outside tenant scope (a lead decision).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sys
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import Clock

log = logging.getLogger("thallo.api.sessions")

IDLE_TIMEOUT = timedelta(minutes=15)
ABSOLUTE_LIFETIME = timedelta(hours=12)
CODE_TTL = timedelta(minutes=10)
CODE_DIGITS = 6
MAX_CODE_ATTEMPTS = 5


class SessionKind(StrEnum):
    INTERACTIVE = "interactive"  # a person in a browser
    API_TOKEN = "api_token"  # a script or integration; never human
    SERVICE = "service"  # a job or worker; never human


@dataclass(frozen=True)
class Shown:
    """The root hash the server put on a review card, and when."""

    root_hash: str | None
    at: datetime


@dataclass(frozen=True)
class Session:
    id: str
    token_hash: str
    kind: SessionKind
    csrf: str
    created_at: datetime
    last_seen: datetime
    authenticated: bool = False
    identity_id: str | None = None
    tenant_id: str | None = None
    app_user_id: str | None = None
    shown: dict[str, Shown] = field(default_factory=dict)

    @property
    def is_member(self) -> bool:
        return self.authenticated and self.tenant_id is not None and self.app_user_id is not None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore(Protocol):
    def create(self, kind: SessionKind = SessionKind.INTERACTIVE) -> tuple[str, Session]: ...
    def by_token(self, token: str | None) -> Session | None: ...
    def by_id(self, session_id: str) -> Session | None: ...
    def touch(self, session: Session) -> Session: ...
    def save(self, session: Session) -> None: ...
    def rotate(self, session: Session, **changes: object) -> tuple[str, Session]: ...
    def delete(self, session: Session) -> None: ...
    def is_live(self, session: Session) -> bool: ...


class InMemorySessionStore:
    def __init__(self, clock: Clock, *, idle: timedelta = IDLE_TIMEOUT,
                 lifetime: timedelta = ABSOLUTE_LIFETIME) -> None:
        self.clock = clock
        self.idle = idle
        self.lifetime = lifetime
        self._by_hash: dict[str, Session] = {}
        self._by_id: dict[str, str] = {}

    def _new(self, kind: SessionKind, base: Session | None = None, **changes: object) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        now = self.clock.now()
        fresh = Session(id=secrets.token_hex(16), token_hash=_hash_token(token), kind=kind,
                        csrf=secrets.token_urlsafe(32), created_at=now, last_seen=now)
        if base is not None:
            fresh = replace(fresh, authenticated=base.authenticated, identity_id=base.identity_id,
                            tenant_id=base.tenant_id, app_user_id=base.app_user_id,
                            created_at=base.created_at if base.authenticated else now)
        fresh = replace(fresh, **changes)  # type: ignore[arg-type]
        self.save(fresh)
        return token, fresh

    def create(self, kind: SessionKind = SessionKind.INTERACTIVE) -> tuple[str, Session]:
        return self._new(kind)

    def is_live(self, session: Session) -> bool:
        now = self.clock.now()
        stored = self._by_hash.get(session.token_hash)
        return (stored is not None and stored.id == session.id and now - stored.last_seen < self.idle
                and now - stored.created_at < self.lifetime)

    def by_token(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        s = self._by_hash.get(_hash_token(token))
        if s is None:
            return None
        if not self.is_live(s):
            self.delete(s)
            return None
        return s

    def by_id(self, session_id: str) -> Session | None:
        h = self._by_id.get(session_id)
        s = self._by_hash.get(h) if h else None
        return s if s is not None and self.is_live(s) else None

    def touch(self, session: Session) -> Session:
        fresh = replace(session, last_seen=self.clock.now())
        self.save(fresh)
        return fresh

    def save(self, session: Session) -> None:
        self._by_hash[session.token_hash] = session
        self._by_id[session.id] = session.token_hash

    def rotate(self, session: Session, **changes: object) -> tuple[str, Session]:
        """A new token, id and CSRF token for the same person; the old token stops working."""
        self.delete(session)
        return self._new(session.kind, session, **changes)

    def delete(self, session: Session) -> None:
        self._by_hash.pop(session.token_hash, None)
        self._by_id.pop(session.id, None)


# ---------------------------------------------------------------------- one-time codes

class CodeSender(Protocol):
    """Delivers a login code. The only implementation in this package prints it to the console."""

    def send(self, email: str, code: str) -> None: ...


class ConsoleCodeSender:
    """Development only: prints the code to this process's stderr. Never sends an email."""

    def __init__(self, stream=None) -> None:  # noqa: ANN001
        self.stream = stream if stream is not None else sys.stderr

    def send(self, email: str, code: str) -> None:
        print(f"[thallo dev login] code for {email}: {code}", file=self.stream, flush=True)


class NoCodeSender:
    """No sender configured: no code goes anywhere, so nobody can finish signing in (fails closed)."""

    def send(self, email: str, code: str) -> None:
        log.warning("login code requested but no code sender is configured")


@dataclass
class _Pending:
    code_mac: str
    expires_at: datetime
    attempts: int = 0


class LoginCodes:
    """Codes are bound to the anonymous session that asked and to the email. Only an HMAC of the
    code is kept, under a key that exists only in this process."""

    def __init__(self, clock: Clock, *, ttl: timedelta = CODE_TTL, max_attempts: int = MAX_CODE_ATTEMPTS) -> None:
        self.clock = clock
        self.ttl = ttl
        self.max_attempts = max_attempts
        self._key = secrets.token_bytes(32)
        self._pending: dict[tuple[str, str], _Pending] = {}

    def _mac(self, code: str) -> str:
        return hmac.new(self._key, code.encode(), hashlib.sha256).hexdigest()

    def issue(self, session_id: str, email: str) -> str:
        code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
        self._pending[(session_id, email)] = _Pending(self._mac(code), self.clock.now() + self.ttl)
        return code

    def check(self, session_id: str, email: str, code: str) -> bool:
        key = (session_id, email)
        p = self._pending.get(key)
        if p is None:
            return False
        if self.clock.now() >= p.expires_at or p.attempts >= self.max_attempts:
            self._pending.pop(key, None)
            return False
        p.attempts += 1
        if hmac.compare_digest(p.code_mac, self._mac(code)):
            self._pending.pop(key, None)
            return True
        if p.attempts >= self.max_attempts:
            self._pending.pop(key, None)
        return False


class RateLimiter:
    """Sliding window per key, in memory. Keys are hashes of an email or a session id; no IP."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._hits: dict[str, deque[datetime]] = {}

    def allow(self, key: str, limit: int, window: timedelta) -> bool:
        now = self.clock.now()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def email_key(kind: str, email: str) -> str:
    return f"{kind}:{hashlib.sha256(email.encode()).hexdigest()}"


# ---------------------------------------------------------------------- the verifier

class ApiSessionVerifier:
    """The real `SessionVerifier`: True only for a live INTERACTIVE session that verified a login
    code and is bound to exactly this membership (user id) in exactly this tenant."""

    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions

    def is_live_human_session(self, actor: HumanActor) -> bool:
        if not isinstance(actor, HumanActor):
            return False
        s = self.sessions.by_id(actor.session_id)
        return (s is not None and s.kind is SessionKind.INTERACTIVE and s.authenticated
                and s.app_user_id == actor.id and s.tenant_id == actor.tenant_id and self.sessions.is_live(s))


__all__ = [
    "ABSOLUTE_LIFETIME", "CODE_TTL", "IDLE_TIMEOUT", "ApiSessionVerifier", "CodeSender", "ConsoleCodeSender",
    "InMemorySessionStore", "LoginCodes", "NoCodeSender", "RateLimiter", "Session", "SessionKind",
    "SessionStore", "Shown", "email_key",
]
