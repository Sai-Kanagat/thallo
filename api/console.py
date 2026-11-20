"""The operator console (WP-24): every client of the signed-in operator on one screen.

  GET /api/console    queue health, cost against cap, breaker trips, prompt versions, eval results,
                      token expiries, per tenant of that operator only

A person in a live browser session only; API tokens are refused (they are scoped to one tenant).
Which tenants: owner-side (`api.operators`), from the session's identity, never from the request.
Each tenant's numbers are then read as the app role inside that tenant (row-level security).
Nothing is invented: no cap set reads "no cap set", never a percentage of zero or of a default.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Request

from api.context import current_session, services
from api.errors import ApiError, forbidden, unauthenticated
from api.sessions import SessionKind
from thallo.contracts.models import ChannelAccount, CostEntry, Incident, Job, PromptVersion, Tenant
from thallo.contracts.states import JobState
from thallo.learning.records import EvalRun
from thallo.trust.records import BreakerTrip, CostCap

router = APIRouter(prefix="/api")

STUCK_AFTER = timedelta(minutes=15)
EXPIRY_WINDOW = timedelta(days=30)
TRIP_DAYS = 7
ALL = "*"


def _money(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))


def _queue(jobs: list[Job], now: datetime) -> dict:
    counts = {s.value: 0 for s in JobState}
    for j in jobs:
        counts[j.state.value] += 1
    due = [j for j in jobs if j.state is JobState.QUEUED and j.run_at <= now]
    stuck = [j for j in jobs if j.state is JobState.RUNNING and j.locked_at is not None
             and now - j.locked_at > STUCK_AFTER]
    oldest = min((j.run_at for j in due), default=None)
    status = "ok"
    if counts[JobState.DEAD.value] or stuck:
        status = "attention"
    elif oldest is not None and now - oldest > STUCK_AFTER:
        status = "behind"
    return {"status": status, "counts": counts, "due_waiting": len(due), "stuck_running": len(stuck),
            "oldest_due_minutes": int((now - oldest).total_seconds() // 60) if oldest is not None else None}


def _cost(entries: list[CostEntry], caps: list[CostCap], now: datetime) -> dict:
    day = now.astimezone(UTC).date()
    today: dict[str, Decimal] = {}
    month = Decimal(0)
    for e in entries:
        d = e.occurred_at.astimezone(UTC).date()
        if d == day:
            today[e.provider] = today.get(e.provider, Decimal(0)) + Decimal(e.cost_eur)
        if (d.year, d.month) == (day.year, day.month):
            month += Decimal(e.cost_eur)
    day_caps = {c.provider: Decimal(c.cap_eur) for c in caps if c.scope == "day"}
    total = sum(today.values(), Decimal(0))
    lines = []
    for provider in sorted(set(today) | {p for p in day_caps if p != ALL}):
        spent = today.get(provider, Decimal(0))
        cap = day_caps.get(provider)
        lines.append({"provider": provider, "spent_eur": _money(spent),
                      "cap_eur": _money(cap) if cap is not None else None,
                      "pct": int(spent * 100 / cap) if cap else None})
    cap_all = day_caps.get(ALL)
    return {"today_eur": _money(total), "month_eur": _money(month),
            "day_cap_eur": _money(cap_all) if cap_all is not None else None,
            "day_pct": int(total * 100 / cap_all) if cap_all else None,
            "cap_note": None if day_caps else "no cap set", "providers": lines}


def _tenant_view(tx, svc, tenant_id: str, name: str, operator: str, now: datetime) -> dict:  # noqa: ANN001
    tenant = tx.get(Tenant, tenant_id)
    trips = [t for t in tx.list_all(BreakerTrip) if (now.date() - t.day).days < TRIP_DAYS]
    prompts = tx.list_all(PromptVersion)
    latest: dict[str, PromptVersion] = {}
    for p in prompts:
        if p.activated_at is not None and (p.name not in latest or p.version > latest[p.name].version):
            latest[p.name] = p
    runs = sorted(tx.list_all(EvalRun), key=lambda r: r.ran_at, reverse=True)[:8]
    expiring = []
    for a in tx.list_all(ChannelAccount):
        if a.token_expires_at is not None and a.token_expires_at - now < EXPIRY_WINDOW:
            expiring.append({"kind": "channel", "label": f"{a.channel.value} {a.handle}",
                             "expires_at": a.token_expires_at.isoformat(),
                             "days_left": (a.token_expires_at - now).days, "expired": a.token_expires_at <= now})
    for t in svc.tokens.list(tenant_id):
        if t.revoked_at is None and t.expires_at - now < EXPIRY_WINDOW:
            expiring.append({"kind": "api_token", "label": f"API token {t.name}",
                             "expires_at": t.expires_at.isoformat(),
                             "days_left": (t.expires_at - now).days, "expired": t.expires_at <= now})
    expiring.sort(key=lambda x: x["expires_at"])
    return {
        "tenant_id": tenant_id, "name": name, "operator": operator, "paused": bool(tenant and tenant.paused),
        "open_incidents": sum(1 for i in tx.list_all(Incident) if i.closed_at is None),
        "queue": _queue(tx.list_all(Job), now),
        "cost": _cost(tx.list_all(CostEntry), tx.list_all(CostCap), now),
        "breakers": [{"id": t.id, "scope": t.scope, "subject": t.subject, "day": t.day.isoformat(),
                      "cap_eur": str(t.cap_eur), "spent_eur": str(t.spent_eur), "reset": t.reset_at is not None}
                     for t in sorted(trips, key=lambda t: t.tripped_at, reverse=True)],
        "prompts": [{"name": p.name, "version": p.version, "role": p.role.value, "model_id": p.model_id,
                     "eval_passed": p.eval_passed, "activated_at": p.activated_at.isoformat()}
                    for p in sorted(latest.values(), key=lambda p: p.name)],
        "evals": [{"change_kind": r.change_kind.value, "change_name": r.change_name, "passed": r.passed,
                   "cases": r.cases, "failed": r.failed, "flagged": r.flagged, "not_run": r.not_run,
                   "ran_at": r.ran_at.isoformat()} for r in runs],
        "expiries": expiring,
    }


@router.get("/console")
def console(request: Request) -> dict:
    if request.headers.get("authorization") is not None:
        raise ApiError(403, "token_refused", "API tokens cannot open the operator console")
    session = current_session(request)
    if session is None or not session.authenticated or session.identity_id is None:
        raise unauthenticated()
    svc = services(request)
    if session.kind is not SessionKind.INTERACTIVE or not svc.sessions.is_live(session):
        raise unauthenticated()
    tenants = svc.operators.tenants_for(session.identity_id)
    if not tenants:
        raise forbidden("you do not run an operator")
    now = svc.clock.now()
    out = []
    for t in tenants:
        with svc.backend.tenant(t.tenant_id) as tx:
            out.append(_tenant_view(tx, svc, t.tenant_id, t.tenant_name, t.operator_name, now))
    return {"generated_at": now.isoformat(), "tenants": out}


__all__ = ["router"]
