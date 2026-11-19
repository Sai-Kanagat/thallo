"""The lead desk screen's API (WP-13). Every route goes through `member`; the tenant and the acting
person come only from the session. Sending needs a verified live session: the desk calls
`require_human` with the API's session verifier.

  GET  /api/leads                              the grid: fit and heat as two numbers, route, clock
  GET  /api/leads/{id}                         one lead: questions, fit table, drafts, consent, clock
  POST /api/leads/{id}/answers                 {answers: {key: value}}
  POST /api/leads/{id}/book                    {starts_at, kind}
  POST /api/leads/{id}/outcome                 {kind: held|no_show|won|lost, amount?, reason?}
  POST /api/leads/messages/{mid}/edit          {body}
  POST /api/leads/messages/{mid}/discard
  POST /api/leads/messages/{mid}/send          {body_sha256}: one tap, one message
  POST /api/leads/messages/{mid}/sent          the person sent it from their own app
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from api.context import member, services
from api.errors import ApiError, bad_request, not_found
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts.enums import Capability, LossReason, UserRole
from thallo.contracts.ids import is_id
from thallo.contracts.models import Lead, Message
from thallo.contracts.states import MessageState
from thallo.leads import Desk, DeskError, body_hash
from thallo.leads.drafts import RegistryDrafter
from thallo.leads.records import ContactCandidate, LeadMessageSend
from thallo.leads.scoring import fit as fit_table

router = APIRouter(prefix="/api/leads")

DESK = frozenset({UserRole.FOUNDER, UserRole.OPERATOR, UserRole.APPROVER, UserRole.SELLER})
_STATUS = {"not_found": 404, "not_set_up": 409, "forbidden": 403}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnswersBody(_Body):
    answers: dict[str, str] = Field(max_length=20)


class BookBody(_Body):
    starts_at: AwareDatetime
    kind: str = Field(min_length=1, max_length=40)


class OutcomeBody(_Body):
    kind: str
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)
    reason: LossReason | None = None


class EditBody(_Body):
    body: str = Field(min_length=1, max_length=2000)


class SendBody(_Body):
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _fail(exc: DeskError) -> ApiError:
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


def connection_registry(request: Request) -> ConnectionRegistry:
    svc = services(request)
    conns, _secrets, health = svc.backend.connection_stores()
    return ConnectionRegistry(clock=svc.clock, providers=svc.providers, connections=conns, vault=svc.vault,
                              health=health, mock_tenants=svc.mock_tenants, environ=svc.environ)


def _desk(request: Request, tx: Any, tenant_id: str) -> Desk:
    reg = connection_registry(request)
    reg.ensure_manual(tenant_id, Capability.TEXT)
    return Desk(tx, tenant_id, services(request).clock, drafter=RegistryDrafter(reg))


def _lead_view(d: Desk, lead: Lead, *, full: bool) -> dict[str, Any]:
    person = d.person(lead)
    channel = d.channel_for(person)
    decision = d.consent(person, channel) if channel else None
    view: dict[str, Any] = {
        "id": lead.id, "name": person.display_name or "No name given", "state": lead.state.value,
        "route": lead.route.value if lead.route else None, "fit": lead.fit, "heat": round(lead.heat, 1),
        "clock": d.clock_status(lead), "channel": channel.value if channel else None,
        "may_message": bool(decision and decision.allowed),
        "consent_reason": None if decision is None or decision.allowed else decision.reason,
        "drafts": sum(1 for m in d.tx.list_all(Message, lead_id=lead.id) if m.state is MessageState.DRAFTED),
        "created_at": lead.created_at.isoformat(),
    }
    if not full:
        return view
    play = d.play()
    view["person"] = {"phone": person.phone, "email": person.email}
    view["questions"] = [{"key": q.key, "ask": q.ask, "type": q.type, "options": list(q.options),
                          "answer": lead.answers.get(q.key)} for q in play.questions_for(lead.answers)]
    view["fit_lines"] = [{"rule": r, "points": p}
                         for r, p in fit_table(play, lead.answers, email=person.email, lists=d.lists()).lines]
    msgs = []
    for m in sorted(d.tx.list_all(Message, lead_id=lead.id), key=lambda m: m.created_at):
        rec = d.tx.get(LeadMessageSend, m.id)
        msgs.append({"id": m.id, "kind": m.kind.value, "channel": m.channel.value, "state": m.state.value,
                     "body": m.body, "body_sha256": body_hash(m.body),
                     "send_state": rec.state.value if rec else None,
                     "editable": m.state is MessageState.DRAFTED and m.kind.value == "reply_draft" and rec is None})
    view["messages"] = msgs
    view["unverified_contacts"] = [{"channel": c.channel.value, "value": c.value}
                                   for c in d.tx.list_all(ContactCandidate, lead_id=lead.id)]
    return view


@router.get("")
def list_leads(request: Request) -> dict:
    ctx = member(request, DESK)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        d = _desk(request, tx, ctx.tenant_id)
        try:
            d.setting()
        except DeskError:
            return {"set_up": False, "leads": []}
        leads = [_lead_view(d, x, full=False) for x in tx.list_all(Lead)]
    order = {"hot": 0, "warm": 1, None: 2, "low_fit": 3}
    leads.sort(key=lambda v: (v["state"] in ("won", "lost"), not v["clock"]["overdue"], order.get(v["route"], 2),
                              -v["heat"], v["created_at"]))
    return {"set_up": True, "leads": leads}


@router.get("/{lead_id}")
def get_lead(lead_id: str, request: Request) -> dict:
    ctx = member(request, DESK)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        d = _desk(request, tx, ctx.tenant_id)
        try:
            return _lead_view(d, d.lead(_id(lead_id)), full=True)
        except DeskError as exc:
            raise _fail(exc) from None


def _act(request: Request, fn) -> dict:  # noqa: ANN001
    ctx = member(request, DESK, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        d = _desk(request, tx, ctx.tenant_id)
        try:
            return fn(d, ctx, svc) or {"status": "ok"}
        except DeskError as exc:
            raise _fail(exc) from None


@router.post("/{lead_id}/answers")
def answers(lead_id: str, body: AnswersBody, request: Request) -> dict:
    return _act(request, lambda d, ctx, svc: _lead_view(d, d.record_answers(_id(lead_id), body.answers), full=True))


@router.post("/{lead_id}/book")
def book(lead_id: str, body: BookBody, request: Request) -> dict:
    def go(d, ctx, svc):  # noqa: ANN001, ANN202
        d.book(_id(lead_id), starts_at=body.starts_at, kind=body.kind, actor=ctx.human(), verifier=svc.verifier)
        return _lead_view(d, d.lead(lead_id), full=True)
    return _act(request, go)


@router.post("/{lead_id}/outcome")
def outcome(lead_id: str, body: OutcomeBody, request: Request) -> dict:
    def go(d, ctx, svc):  # noqa: ANN001, ANN202
        d.outcome(_id(lead_id), body.kind, actor=ctx.human(), verifier=svc.verifier, amount=body.amount,
                  reason=body.reason)
        return _lead_view(d, d.lead(lead_id), full=True)
    return _act(request, go)


@router.post("/messages/{message_id}/edit")
def edit(message_id: str, body: EditBody, request: Request) -> dict:
    def go(d, ctx, svc):  # noqa: ANN001, ANN202
        m = d.edit_draft(_id(message_id), body.body)
        return {"status": "edited", "body_sha256": body_hash(m.body)}
    return _act(request, go)


@router.post("/messages/{message_id}/discard")
def discard(message_id: str, request: Request) -> dict:
    return _act(request, lambda d, ctx, svc: d.discard(_id(message_id)) or {"status": "discarded"})


@router.post("/messages/{message_id}/send")
def send(message_id: str, body: SendBody, request: Request) -> dict:
    def go(d, ctx, svc):  # noqa: ANN001, ANN202
        reg = connection_registry(request)
        reg.ensure_manual(ctx.tenant_id, Capability.MESSAGING)
        return d.approve_and_send(_id(message_id), actor=ctx.human(), verifier=svc.verifier, sender=reg,
                                  body_sha256=body.body_sha256)
    if not is_id(message_id):
        raise not_found()
    return _act(request, go)


@router.post("/messages/{message_id}/sent")
def sent(message_id: str, request: Request) -> dict:
    def go(d, ctx, svc):  # noqa: ANN001, ANN202
        d.confirm_sent(_id(message_id), actor=ctx.human(), verifier=svc.verifier)
        return {"status": "sent"}
    return _act(request, go)


__all__ = ["router"]
