"""The week plan over `plan_slot`: seven days of slots by channel, and kill or move.

Every change goes through the PLAN_SLOT state machine. Kill is legal from OPEN or MISSED. Move
changes `starts_at` inside the same week, for an OPEN slot or a FILLED one whose variant is not
yet scheduled or published (moving it would not move what the publisher already holds).

The stage mix compares the live slots of the week (not killed) with the tenant's pillar targets,
summed per funnel stage. It is shown as shares; with no pillars there is no target.

  GET  /api/plan?week=YYYY-MM-DD         the Monday of the week; defaults to this week
  POST /api/plan/slots/{id}/kill
  POST /api/plan/slots/{id}/move         {starts_at}
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict

from api.context import PLAN_READERS, PLANNERS, member, services
from api.errors import bad_request, conflict, not_found
from thallo.contracts.enums import FunnelStage
from thallo.contracts.ids import is_id
from thallo.contracts.models import Pillar, PlanSlot, Variant
from thallo.contracts.states import PLAN_SLOT, IllegalTransition, PlanSlotState, VariantState

router = APIRouter(prefix="/api")

_MOVABLE = frozenset({PlanSlotState.OPEN, PlanSlotState.FILLED})
_HELD = frozenset({VariantState.SCHEDULED, VariantState.PUBLISHED, VariantState.READ_BACK_OK,
                   VariantState.READ_BACK_MISMATCH})


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_at: AwareDatetime


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _week(raw: str | None, today: date) -> date:
    if raw is None:
        return _monday(today)
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        raise bad_request("week must be a date like 2026-01-05") from None
    return _monday(d)


def _slot_view(s: PlanSlot, pillars: dict[str, Pillar]) -> dict:
    p = pillars.get(s.pillar_id or "")
    return {"id": s.id, "channel": s.channel.value, "stage": s.stage.value, "state": s.state.value,
            "starts_at": s.starts_at.isoformat(), "day": s.starts_at.date().isoformat(),
            "pillar": p.name if p else None, "variant_id": s.variant_id,
            "reserved_for_experiment": s.reserved_for_experiment,
            "can_kill": PLAN_SLOT.allowed(s.state, PlanSlotState.KILLED),
            "can_move": s.state in _MOVABLE}


def _mix(slots: list[PlanSlot], pillars: list[Pillar]) -> list[dict]:
    live = [s for s in slots if s.state is not PlanSlotState.KILLED]
    counts = Counter(s.stage for s in live)
    targets: dict[FunnelStage, Decimal] = {}
    for p in pillars:
        targets[p.stage] = targets.get(p.stage, Decimal(0)) + Decimal(p.target_share)
    total_target = sum(targets.values(), Decimal(0))
    out = []
    for stage in FunnelStage:
        actual = (counts[stage] / len(live)) if live else 0.0
        target = float(targets[stage] / total_target) if total_target and stage in targets else None
        out.append({"stage": stage.value, "slots": counts[stage], "share": round(float(actual), 4),
                    "target": round(target, 4) if target is not None else None})
    return out


@router.get("/plan")
def week_plan(request: Request, week: str | None = None) -> dict:
    ctx = member(request, PLAN_READERS)
    svc = services(request)
    start = _week(week, svc.clock.now().date())
    with svc.backend.tenant(ctx.tenant_id) as tx:
        slots = sorted(tx.list_all(PlanSlot, week_start=start), key=lambda s: (s.starts_at, s.channel.value, s.id))
        pillars = tx.list_all(Pillar)
    by_id = {p.id: p for p in pillars}
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    return {"week_start": start.isoformat(), "days": days, "slots": [_slot_view(s, by_id) for s in slots],
            "stage_mix": _mix(slots, pillars), "can_edit": ctx.user.role in PLANNERS}


def _slot(tx, slot_id: str) -> PlanSlot:  # noqa: ANN001
    if not is_id(slot_id):
        raise not_found()
    slot = tx.get(PlanSlot, slot_id)
    if slot is None:
        raise not_found()
    return slot


@router.post("/plan/slots/{slot_id}/kill")
def kill(slot_id: str, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        slot = _slot(tx, slot_id)
        try:
            PLAN_SLOT.check(slot.state, PlanSlotState.KILLED)
        except IllegalTransition:
            raise conflict(f"a {slot.state.value} slot cannot be killed") from None
        tx.update(PlanSlot, slot.id, state=PlanSlotState.KILLED)
    return {"status": "killed", "slot_id": slot_id}


@router.post("/plan/slots/{slot_id}/move")
def move(slot_id: str, body: MoveBody, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        slot = _slot(tx, slot_id)
        if slot.state not in _MOVABLE:
            raise conflict(f"a {slot.state.value} slot cannot be moved")
        if slot.variant_id:
            v = tx.get(Variant, slot.variant_id)
            if v is not None and v.state in _HELD:
                raise conflict("this slot's piece is already scheduled; it cannot be moved here")
        when: datetime = body.starts_at
        if not slot.week_start <= when.date() < slot.week_start + timedelta(days=7):
            raise bad_request("a slot moves only inside its own week")
        tx.update(PlanSlot, slot.id, starts_at=when)
    return {"status": "moved", "slot_id": slot_id, "starts_at": when.isoformat()}


__all__ = ["router"]
