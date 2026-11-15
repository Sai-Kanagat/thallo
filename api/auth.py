"""Login by identity (email) with a one-time code, then a tenant switch across the identity's
memberships. No passwords.

  GET  /api/session              who is signed in, and the CSRF token (starts an anonymous session)
  POST /api/auth/request-code    {email}: always the same answer, known address or not
  POST /api/auth/verify          {email, code}: rotates the session; picks the tenant if there is one
  GET  /api/auth/tenants         the identity's memberships
  POST /api/auth/tenant          {tenant_id}: must be one of those memberships; rotates the session
  POST /api/auth/logout

Every POST needs the session's CSRF token, including the two before sign-in. Requests and checks
are rate limited per email and per session; a limit hit answers the same way for every address.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from api.context import check_csrf, clear_cookie, current_session, services, set_cookie
from api.directory import Membership, normalise_email
from api.errors import ApiError, unauthenticated
from api.sessions import Session, SessionKind, client_keys, email_key

router = APIRouter(prefix="/api")

WINDOW = timedelta(minutes=15)
REQUESTS_PER_EMAIL = 3
REQUESTS_PER_SESSION = 5
REQUESTS_PER_CLIENT = 10
REQUESTS_PER_NETWORK = 30
VERIFIES_PER_SESSION = 10
VERIFIES_PER_CLIENT = 20
VERIFIES_PER_NETWORK = 60

SENT = {"status": "sent", "message": "If that address can sign in, a code is on its way."}


class EmailBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)


class VerifyBody(EmailBody):
    code: str = Field(min_length=8, max_length=12)


class TenantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=64)


def _limited() -> ApiError:
    return ApiError(429, "rate_limited", "too many attempts; wait a few minutes and try again")


def _membership_view(m: Membership) -> dict:
    return {"tenant_id": m.tenant_id, "tenant_name": m.tenant_name, "role": m.role.value,
            "display_name": m.display_name}


def _session_view(request: Request, s: Session) -> dict:
    svc = services(request)
    out: dict = {"authenticated": s.authenticated, "csrf_token": s.csrf, "tenant": None}
    if s.is_member:
        names = {m.tenant_id: m for m in svc.directory.memberships(s.identity_id or "")}
        m = names.get(s.tenant_id or "")
        if m is not None:
            out["tenant"] = _membership_view(m)
    return out


@router.get("/session")
def get_session(request: Request, response: Response) -> dict:
    svc = services(request)
    s = current_session(request)
    if s is None:
        token, s = svc.sessions.create(SessionKind.INTERACTIVE)
        set_cookie(response, token)
    return _session_view(request, s)


def _anonymous(request: Request) -> Session:
    s = current_session(request)
    check_csrf(request, s)
    assert s is not None
    if s.kind is not SessionKind.INTERACTIVE:
        raise unauthenticated()
    return s


def _client_ok(request: Request, kind: str, per_client: int, per_network: int) -> bool:
    """The backstop is per client and per network, never one shared bucket, so one source cannot
    lock everyone out. See `client_keys` for how the client address is read."""
    svc = services(request)
    client, network = client_keys(request.client.host if request.client else None,
                                  request.headers.get("x-forwarded-for"), trust_proxy=svc.trust_proxy)
    return (svc.limits.allow(f"{kind}:{client}", per_client, WINDOW)
            and svc.limits.allow(f"{kind}:{network}", per_network, WINDOW))


@router.post("/auth/request-code", status_code=202)
def request_code(body: EmailBody, request: Request) -> dict:
    svc = services(request)
    s = _anonymous(request)
    email = normalise_email(body.email)
    if email is None:
        raise ApiError(422, "invalid_request", "enter a valid email address")
    if not (_client_ok(request, "req", REQUESTS_PER_CLIENT, REQUESTS_PER_NETWORK)
            and svc.limits.allow(email_key("req", email), REQUESTS_PER_EMAIL, WINDOW)
            and svc.limits.allow(f"req-session:{s.id}", REQUESTS_PER_SESSION, WINDOW)):
        raise _limited()
    if svc.lockout.locked(email_key("fail", email)):
        return SENT  # locked after repeated failures: no new code, and the same answer
    identity = svc.directory.find_identity(email)
    if identity is not None and svc.directory.memberships(identity.id):
        svc.sender.send(email, svc.codes.issue(s.id, email))
    return SENT  # the same words, known address or not


@router.post("/auth/verify")
def verify(body: VerifyBody, request: Request, response: Response) -> dict:
    svc = services(request)
    s = _anonymous(request)
    email = normalise_email(body.email)
    if email is None:
        raise ApiError(400, "invalid_code", "that code is not valid; request a new one")
    # No per-email limit here: a person holding a valid code is never blocked by someone else's
    # attempts on the same address. Each code has its own five-attempt cap; failures per email
    # drive the lockout on new codes (`FailureLockout`).
    if not (_client_ok(request, "verify", VERIFIES_PER_CLIENT, VERIFIES_PER_NETWORK)
            and svc.limits.allow(f"verify-session:{s.id}", VERIFIES_PER_SESSION, WINDOW)):
        raise _limited()
    if not svc.codes.check(s.id, email, body.code.strip()):
        svc.lockout.fail(email_key("fail", email))
        raise ApiError(400, "invalid_code", "that code is not valid; request a new one")
    svc.lockout.clear(email_key("fail", email))
    identity = svc.directory.find_identity(email)
    memberships = svc.directory.memberships(identity.id) if identity is not None else []
    if identity is None or not memberships:
        raise ApiError(400, "invalid_code", "that code is not valid; request a new one")
    only = memberships[0] if len(memberships) == 1 else None
    try:
        token, fresh = svc.sessions.rotate(
            s, authenticated=True, identity_id=identity.id, tenant_id=only.tenant_id if only else None,
            app_user_id=only.app_user_id if only else None, shown={})
    except PermissionError:
        raise unauthenticated() from None
    set_cookie(response, token)
    return {**_session_view(request, fresh), "memberships": [_membership_view(m) for m in memberships]}


@router.get("/auth/tenants")
def tenants(request: Request) -> dict:
    s = current_session(request)
    if s is None or not s.authenticated:
        raise unauthenticated()
    return {"memberships": [_membership_view(m) for m in services(request).directory.memberships(s.identity_id or "")]}


@router.post("/auth/tenant")
def switch_tenant(body: TenantBody, request: Request, response: Response) -> dict:
    svc = services(request)
    s = current_session(request)
    check_csrf(request, s)
    if s is None or not s.authenticated:
        raise unauthenticated()
    choice = next((m for m in svc.directory.memberships(s.identity_id or "") if m.tenant_id == body.tenant_id), None)
    if choice is None:
        raise ApiError(404, "not_found", "not found")  # the same answer as a tenant that does not exist
    try:
        token, fresh = svc.sessions.rotate(s, tenant_id=choice.tenant_id, app_user_id=choice.app_user_id, shown={})
    except PermissionError:
        raise unauthenticated() from None
    set_cookie(response, token)
    return _session_view(request, fresh)


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    svc = services(request)
    s = current_session(request)
    check_csrf(request, s)
    if s is not None:
        svc.sessions.delete(s)
    clear_cookie(response)
    return {"status": "signed_out"}


__all__ = ["router"]
