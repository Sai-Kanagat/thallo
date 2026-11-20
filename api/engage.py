"""The engagement inbox's API (WP-23). Every route goes through `member`; the tenant and the acting
person come only from the session. There is no send route: a person copies the suggestion or opens
the item in its platform, replies there, and marks it handled.

  GET  /api/engage                       open items, flagged first (?all=1 adds closed ones)
  POST /api/engage                       a person pastes a comment or message (manual mode)
  POST /api/engage/{id}/acknowledge      a person looked at a flagged item; a suggestion follows
  POST /api/engage/{id}/edit             {reply}
  POST /api/engage/{id}/handled          the person replied in the platform
  POST /api/engage/{id}/dismiss
  POST /api/engage/{id}/lead             {phone?, email?}: hand over to the lead desk
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from api.context import member, services
from api.errors import ApiError, bad_request, not_found
from api.leads import DESK, _desk, connection_registry
from thallo.contracts.enums import Capability, Channel, Language
from thallo.contracts.ids import is_id
from thallo.contracts.models import Tenant
from thallo.engage import EngageItem, Inbox, InboxError, Incoming, Source
from thallo.engage.records import FLAGGED
from thallo.engage.suggest import RegistrySuggester

router = APIRouter(prefix="/api/engage")
_STATUS = {"not_found": 404, "not_set_up": 409, "forbidden": 403}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasteBody(_Body):
    channel: Channel
    source: Source = Source.COMMENT
    text: str = Field(min_length=1, max_length=4000)
    author: str | None = Field(default=None, max_length=80)
    permalink: str | None = Field(default=None, max_length=1700)
    external_id: str | None = Field(default=None, max_length=200)
    received_at: AwareDatetime | None = None


class EditBody(_Body):
    reply: str = Field(min_length=1, max_length=1500)


class LeadBody(_Body):
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)


def _fail(exc: InboxError) -> ApiError:
    status = _STATUS.get(exc.code)
    if status == 404:
        return not_found()
    if status is None and exc.code.startswith("bad_"):
        return bad_request(str(exc))
    return ApiError(status or 409, exc.code, str(exc))


def _id(raw: str) -> str:
    if not is_id(raw):
        raise not_found()
    return raw


def _inbox(request: Request, tx: Any, tenant_id: str) -> Inbox:
    reg = connection_registry(request)
    reg.ensure_manual(tenant_id, Capability.TEXT)
    try:
        tenant = tx.get(Tenant, tenant_id)
        language = tenant.default_language if tenant is not None else Language.EN
    except Exception:  # noqa: BLE001 - a tenant row the transaction cannot read: plain English
        language = Language.EN
    return Inbox(tx, tenant_id, services(request).clock, language=language, suggester=RegistrySuggester(reg))


def view(row: EngageItem) -> dict[str, Any]:
    return {"id": row.id, "channel": row.channel.value, "source": row.source.value, "author": row.author,
            "text": row.text, "permalink": row.permalink, "received_at": row.received_at.isoformat(),
            "category": row.category.value, "reasons": row.reasons, "state": row.state.value,
            "flagged": row.category in FLAGGED, "acknowledged": row.acknowledged_by is not None,
            "suggested_reply": row.suggested_reply, "suggested_by_model": row.suggestion_model is not None,
            "lead_id": row.lead_id}


@router.get("")
def list_items(request: Request, all: int = 0) -> dict:  # noqa: A002
    ctx = member(request, DESK)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        rows = Inbox(tx, ctx.tenant_id, services(request).clock).items(include_closed=bool(all))
    return {"items": [view(r) for r in rows]}


def _act(request: Request, fn) -> dict:  # noqa: ANN001
    ctx = member(request, DESK, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        inbox = _inbox(request, tx, ctx.tenant_id)
        try:
            return view(fn(inbox, ctx, svc, tx))
        except InboxError as exc:
            raise _fail(exc) from None


@router.post("")
def paste(body: PasteBody, request: Request) -> dict:
    def go(inbox, ctx, svc, tx):  # noqa: ANN001, ANN202
        from thallo.contracts.ids import new_id
        item = Incoming(channel=body.channel, source=body.source, text=body.text, author=body.author,
                        permalink=body.permalink, external_id=body.external_id or f"manual:{new_id(svc.clock)}",
                        received_at=body.received_at or svc.clock.now())
        return inbox.ingest(item)
    return _act(request, go)


@router.post("/{item_id}/acknowledge")
def acknowledge(item_id: str, request: Request) -> dict:
    return _act(request, lambda i, ctx, svc, tx: i.acknowledge(_id(item_id), actor=ctx.human(),
                                                               verifier=svc.verifier))


@router.post("/{item_id}/edit")
def edit(item_id: str, body: EditBody, request: Request) -> dict:
    return _act(request, lambda i, ctx, svc, tx: i.edit_suggestion(_id(item_id), body.reply))


@router.post("/{item_id}/handled")
def handled(item_id: str, request: Request) -> dict:
    return _act(request, lambda i, ctx, svc, tx: i.handled(_id(item_id), actor=ctx.human(), verifier=svc.verifier))


@router.post("/{item_id}/dismiss")
def dismiss(item_id: str, request: Request) -> dict:
    return _act(request, lambda i, ctx, svc, tx: i.dismiss(_id(item_id), actor=ctx.human(), verifier=svc.verifier))


@router.post("/{item_id}/lead")
def to_lead(item_id: str, body: LeadBody, request: Request) -> dict:
    def go(inbox, ctx, svc, tx):  # noqa: ANN001, ANN202
        desk = _desk(request, tx, ctx.tenant_id)
        return inbox.hand_to_leads(_id(item_id), desk, actor=ctx.human(), verifier=svc.verifier,
                                   phone=body.phone, email=body.email)
    return _act(request, go)
