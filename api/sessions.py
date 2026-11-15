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
import ipaddress
import logging
import secrets
import sys
import threading
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
CODE_DIGITS = 8
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
    def touch(self, session: Session) -> Session | None: ...
    def save(self, session: Session) -> bool: ...
    def rotate(self, session: Session, **changes: object) -> tuple[str, Session]: ...
    def delete(self, session: Session) -> None: ...
    def is_live(self, session: Session) -> bool: ...


class InMemorySessionStore:
    """Every read and write takes one lock. `save` and `touch` are compare-and-set: they write only
    while the stored entry for that token still exists with the same session id, so a touch racing a
    logout or a rotation can never bring a revoked session back. Revoked token hashes and ids are
    kept until their absolute lifetime ends and are refused by `by_token` and `by_id`.
    (There is no Postgres session store yet; a durable one must keep the same guarantee with a
    conditional UPDATE ... WHERE token_hash = %s AND id = %s AND revoked_at IS NULL.)"""

    def __init__(self, clock: Clock, *, idle: timedelta = IDLE_TIMEOUT,
                 lifetime: timedelta = ABSOLUTE_LIFETIME) -> None:
        self.clock = clock
        self.idle = idle
        self.lifetime = lifetime
        self._lock = threading.RLock()
        self._by_hash: dict[str, Session] = {}
        self._by_id: dict[str, str] = {}
        self._revoked: dict[str, datetime] = {}  # token hash or session id -> keep until

    def _prune(self) -> None:
        now = self.clock.now()
        for k in [k for k, until in self._revoked.items() if until <= now]:
            del self._revoked[k]

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
        self._by_hash[fresh.token_hash] = fresh
        self._by_id[fresh.id] = fresh.token_hash
        return token, fresh

    def create(self, kind: SessionKind = SessionKind.INTERACTIVE) -> tuple[str, Session]:
        with self._lock:
            return self._new(kind)

    def _live(self, stored: Session | None, session: Session) -> bool:
        now = self.clock.now()
        return (stored is not None and stored.id == session.id and session.token_hash not in self._revoked
                and session.id not in self._revoked and now - stored.last_seen < self.idle
                and now - stored.created_at < self.lifetime)

    def is_live(self, session: Session) -> bool:
        with self._lock:
            return self._live(self._by_hash.get(session.token_hash), session)

    def by_token(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        h = _hash_token(token)
        with self._lock:
            self._prune()
            if h in self._revoked:
                return None
            s = self._by_hash.get(h)
            if s is None:
                return None
            if not self._live(s, s):
                self._revoke(s)
                return None
            return s

    def by_id(self, session_id: str) -> Session | None:
        with self._lock:
            if session_id in self._revoked:
                return None
            h = self._by_id.get(session_id)
            s = self._by_hash.get(h) if h else None
            return s if s is not None and self._live(s, s) else None

    def _cas(self, session: Session) -> bool:
        stored = self._by_hash.get(session.token_hash)
        if stored is None or stored.id != session.id or session.token_hash in self._revoked \
                or session.id in self._revoked:
            return False
        self._by_hash[session.token_hash] = session
        return True

    def touch(self, session: Session) -> Session | None:
        """None when the session was revoked or rotated meanwhile."""
        with self._lock:
            fresh = replace(session, last_seen=self.clock.now())
            return fresh if self._cas(fresh) else None

    def save(self, session: Session) -> bool:
        """Compare-and-set; False (nothing written) when the session is gone."""
        with self._lock:
            return self._cas(session)

    def rotate(self, session: Session, **changes: object) -> tuple[str, Session]:
        """A new token, id and CSRF token for the same person; the old token stops working."""
        with self._lock:
            if self._by_hash.get(session.token_hash) is None or session.token_hash in self._revoked:
                raise PermissionError("the session was revoked")
            self._revoke(session)
            return self._new(session.kind, session, **changes)

    def _revoke(self, session: Session) -> None:
        until = session.created_at + self.lifetime
        self._revoked[session.token_hash] = until
        self._revoked[session.id] = until
        self._by_hash.pop(session.token_hash, None)
        self._by_id.pop(session.id, None)

    def delete(self, session: Session) -> None:
        with self._lock:
            self._revoke(session)


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
        self._lock = threading.Lock()

    def _mac(self, code: str) -> str:
        return hmac.new(self._key, code.encode(), hashlib.sha256).hexdigest()

    def issue(self, session_id: str, email: str) -> str:
        code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
        with self._lock:
            self._pending[(session_id, email)] = _Pending(self._mac(code), self.clock.now() + self.ttl)
        return code

    def pending(self, session_id: str, email: str) -> bool:
        p = self._pending.get((session_id, email))
        return p is not None and self.clock.now() < p.expires_at and p.attempts < self.max_attempts

    def check(self, session_id: str, email: str, code: str) -> bool:
        with self._lock:
            return self._check(session_id, email, code)

    def _check(self, session_id: str, email: str, code: str) -> bool:
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
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: timedelta) -> bool:
        with self._lock:
            now = self.clock.now()
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


LOCKOUT_AFTER = 10  # failed verifications per identity, across every session and client
LOCKOUT_BASE = timedelta(minutes=1)
LOCKOUT_MAX = timedelta(hours=1)


class FailureLockout:
    """Counts failed code checks per email (known or not, so it reveals nothing). From the tenth
    failure on, new codes for that email are refused for 1, 2, 4 ... minutes, capped at an hour.
    A code already issued keeps its own five-attempt cap, so the owner holding a valid code can
    still use it; an attacker gets at most five guesses per code at 1 in 10^8 each, and the lockout
    caps how many codes there are to guess at. A success clears the count."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._fails: dict[str, int] = {}
        self._until: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def locked(self, key: str) -> bool:
        with self._lock:
            until = self._until.get(key)
            return until is not None and self.clock.now() < until

    def fail(self, key: str) -> None:
        with self._lock:
            n = self._fails.get(key, 0) + 1
            self._fails[key] = n
            if n >= LOCKOUT_AFTER:
                wait = min(LOCKOUT_BASE * (2 ** (n - LOCKOUT_AFTER)), LOCKOUT_MAX)
                self._until[key] = self.clock.now() + wait

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._until.pop(key, None)


def client_keys(host: str | None, forwarded_for: str | None, *, trust_proxy: bool) -> tuple[str, str]:
    """(client, network) keys for rate limits, hashed, kept only in memory (no table stores an IP).

    The client is the socket peer. X-Forwarded-For is read only when the operator says a proxy is
    in front (THALLO_TRUSTED_PROXY=1), and then only its last entry, the one that proxy appended.
    The network is the /24 for IPv4 and the /64 for IPv6. A peer that is not an IP address (the
    test client reports "testclient") is its own client and its own network."""
    peer = (host or "unknown").strip()
    if trust_proxy and forwarded_for:
        peer = forwarded_for.split(",")[-1].strip() or peer
    try:
        ip = ipaddress.ip_address(peer)
        net = ipaddress.ip_network(f"{ip}/{24 if ip.version == 4 else 64}", strict=False)
        network = str(net)
    except ValueError:
        network = peer
    h = lambda v: hashlib.sha256(v.encode()).hexdigest()  # noqa: E731
    return f"client:{h(peer)}", f"net:{h(network)}"


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
    "ABSOLUTE_LIFETIME", "CODE_TTL", "FailureLockout", "client_keys",
    "IDLE_TIMEOUT", "ApiSessionVerifier", "CodeSender", "ConsoleCodeSender",
    "InMemorySessionStore", "LoginCodes", "NoCodeSender", "RateLimiter", "Session", "SessionKind",
    "SessionStore", "Shown", "email_key",
]
