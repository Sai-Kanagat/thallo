"""What every route needs: the services, the session behind a request, CSRF and role checks.

A route calls `member(request, roles, unsafe=...)`. It finds the session from the cookie, refuses
an anonymous or tenant-less one, checks CSRF on state-changing calls, and re-reads the caller's
`app_user` row inside the session's tenant (row-level security), so a role change or a removed
membership takes effect on the next request. The tenant id always comes from the session.

API tokens (WP-24): a request carrying `Authorization: Bearer thl_...` is a token call. The cookie
is then ignored entirely. The token is resolved owner-side on every call (no cache), must be live
on the Clock, is rate limited per token, may call only routes `api.access` allows for its scopes,
and every call (refusals included) is written to the tenant's audit log before the route runs; if
that write fails the call is refused. The token acts as the person who made it, re-read on every
call, with a session of kind API_TOKEN, which the SessionVerifier never accepts.
"""
from __future__ import annotations

import hmac
import logging
from collections.abc import Collection
from datetime import timedelta
from dataclasses import dataclass, field

from fastapi import Request, Response

from api.backend import Backend
from api.directory import IdentityDirectory
from api.errors import ApiError, forbidden, unauthenticated
from api.media_store import TenantMediaStore
from api.operators import InMemoryOperatorDirectory, OperatorDirectory
from api.sessions import (
    ApiSessionVerifier,
    CodeSender,
    DeviceSigner,
    EmailCeiling,
    FailureLockout,
    InMemorySessionStore,
    LoginCodes,
    NullSender,
    RateLimiter,
    Session,
    SessionKind,
    SessionStore,
)
from api.token_store import ApiToken, InMemoryTokenStore, TokenStore, hash_token, looks_like_token
from thallo.connections.registry import ProviderRegistry
from thallo.connections.vault import Vault
from thallo.contracts.actors import AgentActor, HumanActor
from thallo.contracts.clock import Clock
from thallo.contracts.enums import UserRole
from thallo.contracts.models import AppUser
from thallo.gate.signing import GateSigner
from thallo.tracking.redirector import Redirector
from thallo.trust.audit import AuditConflict

log = logging.getLogger("thallo.api.tokens")

COOKIE = "__Host-thallo_sid"
DEVICE_COOKIE = "__Host-thallo_device"
CSRF_HEADER = "x-csrf-token"
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

REVIEWERS = frozenset({UserRole.FOUNDER, UserRole.APPROVER, UserRole.OPERATOR})
PLANNERS = frozenset({UserRole.FOUNDER, UserRole.OPERATOR})
PLAN_READERS = frozenset({UserRole.FOUNDER, UserRole.OPERATOR, UserRole.APPROVER})
CONNECTION_EDITORS = frozenset({UserRole.FOUNDER, UserRole.OPERATOR})
TOKEN_ADMINS = frozenset({UserRole.FOUNDER, UserRole.OPERATOR})

TOKEN_RATE = 60  # calls per token per minute
TOKEN_WINDOW = timedelta(minutes=1)


@dataclass
class Services:
    clock: Clock
    backend: Backend
    directory: IdentityDirectory
    sender: CodeSender
    media: TenantMediaStore
    signer: GateSigner | None
    providers: ProviderRegistry
    vault: Vault | None
    sessions: SessionStore = None  # type: ignore[assignment]
    codes: LoginCodes = None  # type: ignore[assignment]
    limits: RateLimiter = None  # type: ignore[assignment]
    lockout: FailureLockout = None  # type: ignore[assignment]
    ceiling: EmailCeiling = None  # type: ignore[assignment]
    devices: DeviceSigner = None  # type: ignore[assignment]
    proxy_peers: frozenset[str] = frozenset()  # THALLO_WEB_PROXY_HOSTS; empty: any peer, HMAC decides
    stamp_key: bytes | None = None  # THALLO_PROXY_STAMP_KEY; None: no stamp is ever trusted
    allowed_origins: Collection[str] = ()
    mock_tenants: Collection[str] = ()
    environ: dict[str, str] = field(default_factory=dict)
    redirector: Redirector | None = None  # WP-12 public /r/{slug}; None: every slug is a 404
    tokens: TokenStore = None  # type: ignore[assignment]  # WP-24; owner-side in production
    operators: OperatorDirectory = None  # type: ignore[assignment]  # WP-24; owner-side in production

    def __post_init__(self) -> None:
        if self.sessions is None:
            self.sessions = InMemorySessionStore(self.clock)
        if self.codes is None:
            self.codes = LoginCodes(self.clock)
        if self.limits is None:
            self.limits = RateLimiter(self.clock)
        if self.lockout is None:
            self.lockout = FailureLockout(self.clock)
        if self.ceiling is None:
            self.ceiling = EmailCeiling(self.clock)
        if self.devices is None:
            self.devices = DeviceSigner()
        if self.tokens is None:
            self.tokens = InMemoryTokenStore()
        if self.operators is None:
            self.operators = InMemoryOperatorDirectory()
        self.decoy_sender = NullSender()
        self.verifier = ApiSessionVerifier(self.sessions)


@dataclass(frozen=True)
class Ctx:
    session: Session
    tenant_id: str
    user: AppUser
    token: ApiToken | None = None  # set on a token call; the session is then of kind API_TOKEN

    @property
    def interactive(self) -> bool:
        return self.token is None and self.session.kind is SessionKind.INTERACTIVE

    def human(self) -> HumanActor:
        """Built from the session alone; nothing in the request names the actor."""
        return HumanActor(id=self.user.id, tenant_id=self.tenant_id, session_id=self.session.id)


def services(request: Request) -> Services:
    return request.app.state.services


def current_session(request: Request) -> Session | None:
    svc = services(request)
    s = svc.sessions.by_token(request.cookies.get(COOKIE))
    return svc.sessions.touch(s) if s is not None else None


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, httponly=True, secure=True, samesite="strict", path="/")


def set_device_cookie(response: Response, value: str, max_age: int) -> None:
    response.set_cookie(DEVICE_COOKIE, value, httponly=True, secure=True, samesite="strict", path="/",
                        max_age=max_age)


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")


def check_csrf(request: Request, session: Session | None) -> None:
    """Every state-changing call: the session's token in the header, and a same-site request."""
    if request.method not in UNSAFE:
        return
    sent = request.headers.get(CSRF_HEADER, "")
    if session is None or not sent or not hmac.compare_digest(sent.encode(), session.csrf.encode()):
        raise ApiError(403, "csrf", "the request is missing its CSRF token; reload the page")
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise ApiError(403, "csrf", "cross-site requests are refused")
    origin = request.headers.get("origin")
    allowed = services(request).allowed_origins
    if origin is not None and allowed and origin not in allowed:
        raise ApiError(403, "csrf", "cross-site requests are refused")


def bearer(request: Request) -> str | None:
    """The bearer token, None without an Authorization header; a malformed one is refused."""
    raw = request.headers.get("authorization")
    if raw is None:
        return None
    scheme, _, value = raw.strip().partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not looks_like_token(value):
        raise unauthenticated()
    return value


def resolve_token(request: Request, raw: str) -> ApiToken:
    svc = services(request)
    tok = svc.tokens.by_hash(hash_token(raw))
    if tok is None or not tok.usable(svc.clock.now()):
        raise unauthenticated()
    return tok


def audit_token_call(request: Request, tok: ApiToken, action: str, **payload: object) -> None:
    """One audit entry in its own transaction. Fails closed: no entry, no call."""
    svc = services(request)
    route = request.scope.get("route")
    body = {"method": request.method, "route": getattr(route, "path", "") or "", **payload}
    for _ in range(3):
        try:
            with svc.backend.tenant(tok.tenant_id) as tx:
                tx.audit.append(tok.tenant_id, AgentActor(id=f"api_token:{tok.id}"), action,
                                subject_kind="api_token", subject_id=tok.id, payload=body)
            return
        except AuditConflict:
            continue
        except Exception as exc:  # noqa: BLE001
            log.error("token audit write failed type=%s", type(exc).__name__)
            break
    raise ApiError(503, "audit_unavailable", "the call could not be recorded, so it was not run")


def _token_member(request: Request, raw: str, roles: Collection[UserRole]) -> Ctx:
    from api.access import token_may  # local: access imports token_store only

    svc = services(request)
    tok = resolve_token(request, raw)
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if not svc.limits.allow(f"api_token:{tok.id}", TOKEN_RATE, TOKEN_WINDOW):
        audit_token_call(request, tok, "api_token.refused", reason="rate_limited")
        raise ApiError(429, "rate_limited", "too many calls with this token; wait a minute")
    if template is None or not token_may(tok.scopes, request.method, template):
        audit_token_call(request, tok, "api_token.refused", reason="not_allowed_for_tokens")
        raise ApiError(403, "token_refused", "API tokens cannot do this; a person must do it in the app")
    with svc.backend.tenant(tok.tenant_id) as tx:
        user = tx.get(AppUser, tok.app_user_id)
    if user is None or user.tenant_id != tok.tenant_id:
        raise unauthenticated()
    if user.role not in roles:
        audit_token_call(request, tok, "api_token.refused", reason="role")
        raise forbidden()
    audit_token_call(request, tok, "api_token.call")
    svc.tokens.touch(tok.id, svc.clock.now())
    now = svc.clock.now()
    session = Session(id=f"api-token-{tok.id}", token_hash=tok.token_sha256, kind=SessionKind.API_TOKEN, csrf="",
                      created_at=tok.created_at, last_seen=now, authenticated=True, identity_id=user.identity_id,
                      tenant_id=tok.tenant_id, app_user_id=user.id)
    return Ctx(session=session, tenant_id=tok.tenant_id, user=user, token=tok)


def member(request: Request, roles: Collection[UserRole], *, unsafe: bool = False) -> Ctx:
    raw = bearer(request)
    if raw is not None:
        return _token_member(request, raw, roles)
    session = current_session(request)
    if session is None or not session.is_member:
        raise unauthenticated()
    if unsafe or request.method in UNSAFE:
        check_csrf(request, session)
    svc = services(request)
    assert session.tenant_id is not None and session.app_user_id is not None
    with svc.backend.tenant(session.tenant_id) as tx:
        user = tx.get(AppUser, session.app_user_id)
    if user is None or user.tenant_id != session.tenant_id or user.identity_id != session.identity_id:
        svc.sessions.delete(session)
        raise unauthenticated()
    if user.role not in roles:
        raise forbidden()
    return Ctx(session=session, tenant_id=session.tenant_id, user=user)


__all__ = [
    "COOKIE", "CONNECTION_EDITORS", "DEVICE_COOKIE", "set_device_cookie",
    "CSRF_HEADER", "PLANNERS", "PLAN_READERS", "REVIEWERS", "Ctx", "Services",
    "TOKEN_ADMINS", "audit_token_call", "bearer", "resolve_token",
    "check_csrf", "clear_cookie", "current_session", "member", "services", "set_cookie",
]
