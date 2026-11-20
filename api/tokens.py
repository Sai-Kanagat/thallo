"""API token management (WP-24). A person in a live browser session only; a token never manages tokens.

  GET  /api/tokens                    founder, operator: this tenant's tokens (never the token itself)
  POST /api/tokens                    {name, scopes: [read, draft], days 1..90}: the token, shown once
  POST /api/tokens/{token_id}/revoke  stops working on the next call

Tokens are per tenant (the session's), act as the person who made them, and can never approve,
send, publish, verify a claim or sign spend: `api.access` refuses those routes to every token and
the SessionVerifier refuses token sessions inside them.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from api.context import TOKEN_ADMINS, Ctx, member, services
from api.errors import ApiError, not_found
from api.token_store import ApiToken, TokenScope, hash_token, new_token
from thallo.contracts.ids import is_id, new_id

router = APIRouter(prefix="/api/tokens")

MAX_DAYS = 90
KIND = "api_token"  # audit action prefix and subject kind


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    scopes: list[TokenScope] = Field(min_length=1, max_length=2)
    days: int = Field(default=30, ge=1, le=MAX_DAYS)


def _person(request: Request, *, unsafe: bool = False) -> Ctx:
    if request.headers.get("authorization") is not None:
        raise ApiError(403, "token_refused", "API tokens cannot manage tokens")
    ctx = member(request, TOKEN_ADMINS, unsafe=unsafe)
    if not ctx.interactive or not services(request).verifier.is_live_human_session(ctx.human()):
        raise ApiError(403, "human_only", "only a person in a live session can do this")
    return ctx


def view(t: ApiToken, now) -> dict:  # noqa: ANN001
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "scopes": sorted(s.value for s in t.scopes),
            "created_at": t.created_at.isoformat(), "expires_at": t.expires_at.isoformat(),
            "revoked": t.revoked_at is not None, "expired": now >= t.expires_at,
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None}


@router.get("")
def list_tokens(request: Request) -> dict:
    ctx = _person(request)
    svc = services(request)
    now = svc.clock.now()
    return {"tokens": [view(t, now) for t in svc.tokens.list(ctx.tenant_id)],
            "scopes": [s.value for s in TokenScope], "max_days": MAX_DAYS}


@router.post("", status_code=201)
def create(body: CreateBody, request: Request) -> dict:
    ctx = _person(request, unsafe=True)
    svc = services(request)
    now = svc.clock.now()
    raw = new_token()
    tok = ApiToken(id=new_id(svc.clock), created_at=now, tenant_id=ctx.tenant_id, app_user_id=ctx.user.id,
                   name=body.name.strip() or "token", token_sha256=hash_token(raw), prefix=raw[:10],
                   scopes=frozenset(body.scopes), expires_at=now + timedelta(days=body.days))
    svc.tokens.add(tok)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        tx.audit.append(ctx.tenant_id, ctx.human(), f"{KIND}.created", subject_kind=KIND, subject_id=tok.id,
                        payload={"scopes": sorted(s.value for s in tok.scopes), "days": body.days})
    return {**view(tok, now), "token": raw, "message": "Copy this token now. It is not shown again."}


@router.post("/{token_id}/revoke")
def revoke(token_id: str, request: Request) -> dict:
    ctx = _person(request, unsafe=True)
    if not is_id(token_id):
        raise not_found()
    svc = services(request)
    if not svc.tokens.revoke(ctx.tenant_id, token_id, ctx.user.id, svc.clock.now()):
        raise not_found()
    with svc.backend.tenant(ctx.tenant_id) as tx:
        tx.audit.append(ctx.tenant_id, ctx.human(), f"{KIND}.revoked", subject_kind=KIND, subject_id=token_id)
    return {"status": "revoked"}


__all__ = ["router"]
