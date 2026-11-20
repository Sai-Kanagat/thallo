"""Nurture, booking, pipeline and CRM sync API (WP-14). Tenant and person come only from the session.

  POST /api/nurture/templates                {name, channel, language, body, slot_sources}   approvers
  POST /api/nurture/templates/{id}/approve   once, by hash                                     approvers
  POST /api/nurture/sequences                {name, channel, steps: [{template_id, wait_hours}], ...}
  GET  /api/nurture/sequences
  POST /api/nurture/leads/{id}/enrol         {sequence_id}
  POST /api/nurture/run                      draft the due steps (sends nothing)
  POST /api/nurture/messages/{mid}/send      {body_sha256}: the lead desk's send path, with the nurture guard
  POST /api/nurture/leads/{id}/booking/offer {kind}: slots or a link, read only
  POST /api/nurture/leads/{id}/booking       {kind, starts_at}: the agreed date and time
  GET  /api/nurture/pipeline
  POST /api/nurture/crm/sync                 approvers; HubSpot only with the operator switch, else CSV
  GET  /api/nurture/crm/export.csv           approvers
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from api.context import member, services
from api.errors import ApiError, not_found
from api.leads import DESK, _fail, _id, connection_registry
from thallo.contracts.enums import Capability, Language, MessageChannel, UserRole
from thallo.contracts.ids import is_id
from thallo.leads import DeskError
from thallo.nurture import booking, crm_sync, nurture_desk
from thallo.nurture.pipeline import board
from thallo.nurture.records import NurtureSequence

router = APIRouter(prefix="/api/nurture")
APPROVERS = frozenset({UserRole.FOUNDER, UserRole.OPERATOR, UserRole.APPROVER})


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateBody(_Body):
    name: str = Field(min_length=1, max_length=80)
    channel: MessageChannel
    language: Language
    body: str = Field(min_length=1, max_length=1000)
    slot_sources: dict[str, str] = Field(default_factory=dict, max_length=8)


class Step(_Body):
    template_id: str
    wait_hours: int = Field(ge=0, le=1440)


class SequenceBody(_Body):
    name: str = Field(min_length=1, max_length=80)
    channel: MessageChannel
    steps: list[Step] = Field(min_length=1, max_length=8)
    quiet_start_hour: int = Field(default=20, ge=0, le=23)
    quiet_end_hour: int = Field(default=9, ge=0, le=23)
    max_per_week: int = Field(default=2, ge=1, le=7)
    min_gap_hours: int = Field(default=48, ge=1, le=720)


class EnrolBody(_Body):
    sequence_id: str


class SendBody(_Body):
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OfferBody(_Body):
    kind: str = Field(min_length=1, max_length=40)


class BookBody(_Body):
    kind: str = Field(min_length=1, max_length=40)
    starts_at: AwareDatetime


def _act(request: Request, roles, fn, *, unsafe: bool = True) -> Any:  # noqa: ANN001
    ctx = member(request, roles, unsafe=unsafe)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        d, n = nurture_desk(tx, ctx.tenant_id, svc.clock)
        try:
            return fn(d, n, ctx, svc)
        except DeskError as exc:
            raise _fail(exc) from None


@router.post("/templates")
def new_template(body: TemplateBody, request: Request) -> dict:
    return _act(request, APPROVERS, lambda d, n, ctx, svc: {"id": n.new_template(
        name=body.name, channel=body.channel, language=body.language, body=body.body,
        slot_sources=body.slot_sources).id})


@router.post("/templates/{template_id}/approve")
def approve_template(template_id: str, request: Request) -> dict:
    return _act(request, APPROVERS, lambda d, n, ctx, svc: {"approval_id": n.approve_template(
        _id(template_id), actor=ctx.human(), verifier=svc.verifier).id})


@router.post("/sequences")
def new_sequence(body: SequenceBody, request: Request) -> dict:
    for s in body.steps:
        _id(s.template_id)
    return _act(request, APPROVERS, lambda d, n, ctx, svc: {"id": n.new_sequence(
        name=body.name, channel=body.channel, steps=[(s.template_id, s.wait_hours) for s in body.steps],
        quiet_start_hour=body.quiet_start_hour, quiet_end_hour=body.quiet_end_hour,
        max_per_week=body.max_per_week, min_gap_hours=body.min_gap_hours).id})


@router.get("/sequences")
def sequences(request: Request) -> dict:
    def go(d, n, ctx, svc):  # noqa: ANN001, ANN202
        return {"sequences": [{"id": s.id, "name": s.name, "channel": s.channel.value, "steps": len(s.steps),
                               "max_per_week": s.max_per_week, "quiet": [s.quiet_start_hour, s.quiet_end_hour]}
                              for s in d.tx.list_all(NurtureSequence) if s.active]}
    return _act(request, DESK, go, unsafe=False)


@router.post("/leads/{lead_id}/enrol")
def enrol(lead_id: str, body: EnrolBody, request: Request) -> dict:
    return _act(request, DESK, lambda d, n, ctx, svc: {"enrolment_id": n.enrol(
        _id(lead_id), _id(body.sequence_id)).id})


@router.post("/run")
def run(request: Request) -> dict:
    return _act(request, DESK, lambda d, n, ctx, svc: n.run_due())


@router.post("/messages/{message_id}/send")
def send(message_id: str, body: SendBody, request: Request) -> dict:
    if not is_id(message_id):
        raise not_found()

    def go(d, n, ctx, svc):  # noqa: ANN001, ANN202
        reg = connection_registry(request)
        reg.ensure_manual(ctx.tenant_id, Capability.MESSAGING)
        return d.approve_and_send(message_id, actor=ctx.human(), verifier=svc.verifier, sender=reg,
                                  body_sha256=body.body_sha256)
    try:
        return _act(request, DESK, go)
    except ApiError as exc:
        if exc.code == "sequence_stopped":  # the refusal rolled back; record the stop in its own transaction
            _act(request, DESK, lambda d, n, ctx, svc: n.run_due())
        raise


@router.post("/leads/{lead_id}/booking/offer")
def offer(lead_id: str, body: OfferBody, request: Request) -> dict:
    def go(d, n, ctx, svc):  # noqa: ANN001, ANN202
        reg = connection_registry(request)
        reg.ensure_manual(ctx.tenant_id, Capability.BOOKING)
        return booking.offer(d, _id(lead_id), reg, kind=body.kind)
    return _act(request, DESK, go)


@router.post("/leads/{lead_id}/booking")
def book(lead_id: str, body: BookBody, request: Request) -> dict:
    def go(d, n, ctx, svc):  # noqa: ANN001, ANN202
        b = booking.confirm(d, _id(lead_id), kind=body.kind, starts_at=body.starts_at, actor=ctx.human(),
                            verifier=svc.verifier)
        return {"booking_id": b.id, "kind": b.kind, "starts_at": b.starts_at.isoformat()}
    return _act(request, DESK, go)


@router.get("/pipeline")
def pipeline(request: Request) -> dict:
    def go(d, n, ctx, svc):  # noqa: ANN001, ANN202
        try:
            play = d.setting().play_code
        except DeskError:
            return {"set_up": False}
        return {"set_up": True, **board(d.tx, svc.clock, play)}
    return _act(request, DESK, go, unsafe=False)


@router.post("/crm/sync")
def crm(request: Request) -> dict:
    def go(d, n, ctx, svc):  # noqa: ANN001, ANN202
        reg = connection_registry(request)
        reg.ensure_manual(ctx.tenant_id, Capability.CRM)
        return crm_sync.sync(d, reg, actor=ctx.human(), verifier=svc.verifier)
    return _act(request, APPROVERS, go)


@router.get("/crm/export.csv")
def export(request: Request) -> Response:
    text = _act(request, APPROVERS,
                lambda d, n, ctx, svc: crm_sync.to_csv(d, actor=ctx.human(), verifier=svc.verifier), unsafe=False)
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="thallo-contacts.csv"'})


__all__ = ["router"]
