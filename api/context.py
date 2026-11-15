"""What every route needs: the services, the session behind a request, CSRF and role checks.

A route calls `member(request, roles, unsafe=...)`. It finds the session from the cookie, refuses
an anonymous or tenant-less one, checks CSRF on state-changing calls, and re-reads the caller's
`app_user` row inside the session's tenant (row-level security), so a role change or a removed
membership takes effect on the next request. The tenant id always comes from the session.
"""
from __future__ import annotations

import hmac
from collections.abc import Collection
from dataclasses import dataclass, field

from fastapi import Request, Response

from api.backend import Backend
from api.directory import IdentityDirectory
from api.errors import ApiError, forbidden, unauthenticated
from api.media_store import TenantMediaStore
from api.sessions import (
    ApiSessionVerifier,
    CodeSender,
    InMemorySessionStore,
    LoginCodes,
    RateLimiter,
    Session,
    SessionStore,
)
from thallo.connections.registry import ProviderRegistry
from thallo.connections.vault import Vault
from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import Clock
from thallo.contracts.enums import UserRole
from thallo.contracts.models import AppUser
from thallo.gate.signing import GateSigner

COOKIE = "__Host-thallo_sid"
CSRF_HEADER = "x-csrf-token"
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

REVIEWERS = frozenset({UserRole.FOUNDER, UserRole.APPROVER, UserRole.OPERATOR})
PLANNERS = frozenset({UserRole.FOUNDER, UserRole.OPERATOR})
PLAN_READERS = frozenset({UserRole.FOUNDER, UserRole.OPERATOR, UserRole.APPROVER})
CONNECTION_EDITORS = frozenset({UserRole.FOUNDER, UserRole.OPERATOR})


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
    allowed_origins: Collection[str] = ()
    mock_tenants: Collection[str] = ()
    environ: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sessions is None:
            self.sessions = InMemorySessionStore(self.clock)
        if self.codes is None:
            self.codes = LoginCodes(self.clock)
        if self.limits is None:
            self.limits = RateLimiter(self.clock)
        self.verifier = ApiSessionVerifier(self.sessions)


@dataclass(frozen=True)
class Ctx:
    session: Session
    tenant_id: str
    user: AppUser

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


def member(request: Request, roles: Collection[UserRole], *, unsafe: bool = False) -> Ctx:
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
    "COOKIE", "CONNECTION_EDITORS", "CSRF_HEADER", "PLANNERS", "PLAN_READERS", "REVIEWERS", "Ctx", "Services",
    "check_csrf", "clear_cookie", "current_session", "member", "services", "set_cookie",
]
