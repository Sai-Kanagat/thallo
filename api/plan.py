"""The week plan over `plan_slot`: seven days of slots by channel, and kill or move.

Every change goes through the PLAN_SLOT state machine. Kill is legal from OPEN or MISSED. Move
changes `starts_at` inside the same week, for an OPEN slot or a FILLED one whose variant is not
yet scheduled or published (moving it would not move what the publisher already holds).

The stage mix compares the live slots of the week (not killed) with the tenant's pillar targets,
summed per funnel stage. It is shown as shares; with no pillars there is no target.

  GET  /api/plan?week=YYYY-MM-DD         the Monday of the week; defaults to this week
  POST /api/plan/slots/{id}/kill
  POST /api/plan/slots/{id}/move         {starts_at}
  POST /api/plan/propose                 {week}: the planner proposes that week (WP-17)
  POST /api/plan/mirror                  {week}: mirror the week to the calendar connection (WP-17)

Planned weeks also carry the planner's two reasoning lines, what it chose not to do, its stage
check against the play, and per slot the play, format, target metric, reason and the sealed
forecast (labelled a guess until scored). Killing a planned slot also kills its idea.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict

from api.context import PLAN_READERS, PLANNERS, member, services
from api.errors import bad_request, conflict, not_found
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts.enums import FunnelStage
from thallo.contracts.ids import is_id
from thallo.contracts.models import Forecast, ForecastScore, Idea, Pillar, PlanSlot, Tenant, Variant
from thallo.contracts.states import IDEA, PLAN_SLOT, IdeaState, IllegalTransition, PlanSlotState, VariantState
from thallo.planner import forecast as fc
from thallo.planner.mirror import mirror_week
from thallo.planner.plan import PlanRefused, plan_week
from thallo.planner.records import PlannerMirror, PlannerSlot, PlannerWeek

router = APIRouter(prefix="/api")

_MOVABLE = frozenset({PlanSlotState.OPEN, PlanSlotState.FILLED})
_HELD = frozenset({VariantState.SCHEDULED, VariantState.PUBLISHED, VariantState.READ_BACK_OK,
                   VariantState.READ_BACK_MISMATCH})


class WeekBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    week: date


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


def _planned(tx, slots: list[PlanSlot]) -> dict[str, dict]:  # noqa: ANN001
    """slot id -> the planner's fields, for slots the planner made."""
    extra = {p.slot_id: p for p in tx.list_all(PlannerSlot)}
    if not extra:
        return {}
    forecasts = {f.id: f for f in tx.list_all(Forecast)}
    scores = {s.forecast_id: s for s in tx.list_all(ForecastScore)}
    out = {}
    for s in slots:
        p = extra.get(s.id)
        if p is None:
            continue
        idea = tx.get(Idea, p.idea_id)
        f = forecasts.get(p.forecast_id or "")
        out[s.id] = {"play": p.play_code, "format": p.format, "target_metric": p.target_metric,
                     "metric_day": p.metric_day, "reason": p.reason, "idea": idea.title if idea else None,
                     "forecast": fc.view(f, scores.get(f.id) if f else None),
                     "no_forecast_reason": p.no_forecast_reason}
    return out


def _slot_view(s: PlanSlot, pillars: dict[str, Pillar], planned: dict | None = None) -> dict:
    p = pillars.get(s.pillar_id or "")
    return {"planned": planned,"id": s.id, "channel": s.channel.value, "stage": s.stage.value, "state": s.state.value,
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
        planned = _planned(tx, slots)
        weeks = tx.list_all(PlannerWeek, week_start=start)
        mirrors = sorted(tx.list_all(PlannerMirror, week_start=start), key=lambda m: (m.mirrored_at, m.id))
    by_id = {p.id: p for p in pillars}
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    w = weeks[0] if weeks else None
    last = mirrors[-1] if mirrors else None
    return {"week_start": start.isoformat(), "days": days,
            "slots": [_slot_view(s, by_id, planned.get(s.id)) for s in slots],
            "stage_mix": _mix(slots, pillars), "can_edit": ctx.user.role in PLANNERS,
            "planner": None if w is None else {"play": w.play_code, "reasoning": w.reasoning,
                                               "not_doing": w.not_doing, "stage_check": w.stage_check,
                                               "experiment_slots": w.experiment_slots},
            "mirror": None if last is None else {"provider": last.provider, "status": last.status,
                                                 "mirrored": last.mirrored, "detail": last.detail,
                                                 "at": last.mirrored_at.isoformat()}}


@router.post("/plan/propose")
def propose(body: WeekBody, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        tenant = tx.get(Tenant, ctx.tenant_id)
        if tenant is None:
            raise not_found()
        try:
            planned = plan_week(tx, svc.clock, tenant, body.week)
        except PlanRefused as e:
            raise conflict(" ".join(e.problems)) from None
    return {"status": "planned", "week_start": planned.week.week_start.isoformat(),
            "slots": len(planned.slots), "reasoning": planned.week.reasoning}


@router.post("/plan/mirror")
def mirror(body: WeekBody, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    svc = services(request)
    conns, _secrets, health = svc.backend.connection_stores()
    registry = ConnectionRegistry(clock=svc.clock, providers=svc.providers, connections=conns, vault=svc.vault,
                                  health=health, mock_tenants=svc.mock_tenants, environ=svc.environ)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        row = mirror_week(tx, registry, svc.clock, _week(body.week.isoformat(), body.week))
    return {"status": row.status, "provider": row.provider, "mirrored": row.mirrored, "detail": row.detail}


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
        for p in tx.list_all(PlannerSlot, slot_id=slot.id):
            idea = tx.get(Idea, p.idea_id)
            if idea is not None and IDEA.allowed(idea.state, IdeaState.KILLED):
                tx.update(Idea, idea.id, state=IdeaState.KILLED)
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
