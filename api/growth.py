"""The growth model form and the play library editor (WP-15).

  GET  /api/growth                 the active growth model, the plays, the policy check per play
  POST /api/growth/model           owner answers; a new version, every rate stored as guessed
  POST /api/growth/preview         the allowable cost for unsaved answers (nothing is written)
  POST /api/growth/plays/{code}    edit a play (or a tuned copy `A-<slug>`); refused when it
                                   fails validation, contradicts the region policy, or no policy
                                   is on file

Every number returned is labelled: a rate carries `tag` guessed or measured, and play numbers
carry `evidence: guess`. Nothing here is presented as a benchmark.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from api.context import PLAN_READERS, PLANNERS, member, services
from api.errors import ApiError, bad_request, not_found
from thallo.contracts.models import Play, Tenant
from thallo.growth import model as gm
from thallo.growth import service
from thallo.growth.plays import GUESS_LABEL, library

router = APIRouter(prefix="/api/growth")


class PlayEdits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    channels: list[str] | None = None
    formats: list[str] | None = None
    qualification_questions: list[dict[str, Any]] | None = Field(default=None, max_length=6)
    booked_definition: str | None = None
    pillar_mix: dict[str, str] | None = None
    metrics: list[str] | None = None


def _tenant(tx, tenant_id: str) -> Tenant:  # noqa: ANN001
    t = tx.get(Tenant, tenant_id)
    if t is None:
        raise not_found()
    return t


def _refused(e: service.Refused) -> ApiError:
    return ApiError(422, "refused", " ".join(e.problems[:5]))


@router.get("")
def growth(request: Request) -> dict:
    ctx = member(request, PLAN_READERS)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        tenant = _tenant(tx, ctx.tenant_id)
        plays = {r.code: r for r in tx.list_all(Play)}
        current = service.model_view(service.active_model(tx), tenant, plays)
        views = service.plays_view(tx, svc.clock, tenant)
    return {"currency": tenant.currency, "region_code": tenant.region_code, "model": current, "plays": views,
            "stages": [{"name": n, "label": label, "required": n in gm.REQUIRED} for n, label in gm.STAGES],
            "default_share": str(gm.DEFAULT_SHARE), "measured_after": gm.MEASURED_AFTER,
            "guess_label": GUESS_LABEL, "open_questions": list(library().open_questions_for_sai),
            "can_edit": ctx.user.role in PLANNERS}


@router.post("/preview")
def preview(body: gm.OwnerAnswers, request: Request) -> dict:
    ctx = member(request, PLAN_READERS, unsafe=True)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        tenant = _tenant(tx, ctx.tenant_id)
    stages = gm.guessed_stages(body)
    return {"stages": stages, "summary": gm.summary(stages, body.margin_per_won, body.share_of_margin, tenant.currency)}


@router.post("/model")
def save_model(body: gm.OwnerAnswers, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    svc = services(request)
    if library().get(body.play_code.split("-", 1)[0]) is None:
        raise bad_request(f"there is no play {body.play_code}")
    with svc.backend.tenant(ctx.tenant_id) as tx:
        tenant = _tenant(tx, ctx.tenant_id)
        try:
            row = service.save_model(tx, svc.clock, tenant, body)
        except service.Refused as e:
            raise _refused(e) from None
        plays = {r.code: r for r in tx.list_all(Play)}
        view = service.model_view(row, tenant, plays)
    return {"status": "saved", "model": view}


@router.post("/plays/{code}")
def save_play(code: str, body: PlayEdits, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    svc = services(request)
    edits = body.model_dump(exclude_none=True)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        tenant = _tenant(tx, ctx.tenant_id)
        try:
            row, result = service.save_play(tx, svc.clock, tenant, code, edits)
        except service.Refused as e:
            raise _refused(e) from None
    return {"status": "saved", "code": row.code, "play_id": row.id, "policy": result.view()}


__all__ = ["router"]
