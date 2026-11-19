"""The trust screen (WP-20): the ladder per task type, canaries, breakers, the weekly hand audit.

  GET  /api/trust                          every task type: level, window, bound, what blocks a raise
  POST /api/trust/{task_type}/raise        founder, operator: one level up, on measured evidence only
  POST /api/trust/breakers/{id}/reset      founder, operator: a spend increase, a person only
  GET  /api/trust/audit?week=YYYY-MM-DD    the week's 20 sentences (topped up on read)
  POST /api/trust/audit/{id}               {verdict: correct | wrong, note}

The tenant always comes from the session; the actor is built from the session alone.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from api.context import PLAN_READERS, PLANNERS, REVIEWERS, member, services
from api.errors import ApiError, bad_request, not_found
from thallo.contracts.actors import HumanOnlyViolation
from thallo.contracts.models import AutonomyState, Incident
from thallo.trust import breakers as br
from thallo.trust import hand_audit, ladder
from thallo.trust import service as ts
from thallo.trust.records import AuditVerdict, BreakerTrip

router = APIRouter(prefix="/api/trust")


class MarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: AuditVerdict
    note: str | None = Field(default=None, max_length=500)


def _refuse(exc: Exception) -> ApiError:
    if isinstance(exc, HumanOnlyViolation):
        return ApiError(403, "human_only", "only a person in a live session can do this")
    if isinstance(exc, ts.Refused):
        return ApiError(422, "refused", " ".join(exc.reasons[:6]))
    return bad_request(str(exc)[:300])


def _task_view(tx, clock, st: AutonomyState) -> dict:  # noqa: ANN001
    ev = ts.evidence(tx, clock, st.task_type)
    el = ladder.eligibility(ev)
    _, meta = ts.state(tx, clock, st.task_type)
    return {"task_type": st.task_type, "level": int(st.level), "does_alone": ladder.ALONE[st.level],
            "paused": st.paused, "pause_reason": meta.pause_reason, "window_items": ev.items, "defects": ev.defects,
            "escaped": ev.escaped, "next_level": int(el.target) if el.target is not None else None,
            "can_raise": el.ok, "blocked_by": list(el.reasons), "bound_pct": el.bound_pct,
            "canary_note": el.canary_note, "changed_at": st.changed_at.isoformat()}


@router.get("")
def overview(request: Request) -> dict:
    ctx = member(request, PLAN_READERS)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        tasks = [_task_view(tx, svc.clock, st) for st in sorted(tx.list_all(AutonomyState), key=lambda s: s.task_type)]
        day = br.utc_day(svc.clock.now())
        trips = [{"id": t.id, "scope": t.scope, "subject": t.subject, "cap_eur": str(t.cap_eur),
                  "spent_eur": str(t.spent_eur), "task_type": t.task_type, "day": t.day.isoformat(),
                  "reset": t.reset_at is not None}
                 for t in tx.list_all(BreakerTrip) if (day - t.day).days < 7]
        incidents = [{"id": i.id, "severity": i.severity.value, "title": i.title, "opened_at": i.opened_at.isoformat()}
                     for i in tx.list_all(Incident) if i.closed_at is None]
    return {"tasks": tasks, "breakers": trips, "incidents": incidents[-20:],
            "can_raise": ctx.user.role in PLANNERS}


@router.post("/{task_type}/raise")
def raise_level(task_type: str, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        if not tx.list_all(AutonomyState, task_type=task_type):
            raise not_found()
        try:
            ch = ts.raise_level(tx, svc.clock, task_type, actor=ctx.human(), verifier=svc.verifier)
        except (ts.TrustError, HumanOnlyViolation) as exc:
            raise _refuse(exc) from None
    return {"status": "raised", "from": int(ch.from_level), "to": int(ch.to_level), "reason": ch.reason}


@router.post("/breakers/{trip_id}/reset")
def reset(trip_id: str, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        try:
            br.reset_trip(tx, svc.clock, trip_id, actor=ctx.human(), verifier=svc.verifier)
        except (ts.TrustError, HumanOnlyViolation) as exc:
            raise _refuse(exc) from None
    return {"status": "reset"}


@router.get("/audit")
def audit_week(request: Request, week: date | None = None) -> dict:
    ctx = member(request, REVIEWERS)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        s = hand_audit.summary(tx, svc.clock, week or svc.clock.now().date())
    return {"week_start": s.week_start.isoformat(), "checked": s.checked, "wrong": s.wrong, "bound_pct": s.bound_pct,
            "note": s.note, "sample": hand_audit.SAMPLE,
            "sentences": [{"id": r.id, "position": r.position, "sentence": r.sentence, "verdict": r.verdict.value,
                           "note": r.note} for r in s.sentences]}


@router.post("/audit/{sentence_id}")
def mark(sentence_id: str, body: MarkBody, request: Request) -> dict:
    ctx = member(request, REVIEWERS, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        try:
            row, result = hand_audit.mark(tx, svc.clock, sentence_id, body.verdict, actor=ctx.human(),
                                          verifier=svc.verifier, note=body.note)
        except (ts.TrustError, HumanOnlyViolation) as exc:
            raise _refuse(exc) from None
    return {"status": "marked", "verdict": row.verdict.value,
            "dropped_to": int(result.changes[-1].to_level) if result and result.changes else None}


__all__ = ["router"]
