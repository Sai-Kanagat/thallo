"""Cost breakers: per run and per day, per tenant, per provider and global. They stop spending
and fail closed.

Call `authorize` before any provider call that costs money, then `record_cost` after it. A call is
refused when, counting the estimate:
- the run's spend would pass the tenant's per-run cap (all providers, or that provider's own);
- today's spend (UTC day) would pass the tenant's per-day cap (all providers, or that provider's);
- today's spend across every tenant would pass the operator's global cap (all, or per provider);
- a breaker already tripped today for that subject, or for this run, and no person reset it;
- the global kill switch is on;
- anything at all goes wrong while checking (a read error, a missing reader): fail closed.

A refusal that passes a cap trips the breaker: a `breaker_trip` row, a BREAKER_TRIPPED event and
an audit entry, in the caller's transaction. Commit it even though the call was refused. Two
trips of one task type within a rolling 7 days pause that task type.

Caps a tenant has not set fall back to DEFAULT_CAPS. Raising a cap, or resetting a tripped
breaker, is a spend increase: a named human only (SIGN_SPEND). Lowering a cap needs no one.

Known limit: authorize then record is check-then-act. Two concurrent calls can both pass and
overshoot a cap by one call's cost; the next call is refused. Keep per-call estimates small.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from thallo.contracts.actors import Actor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import BreakerScope, Capability, HumanOnlyAction
from thallo.contracts.events import BreakerPayload, EventName
from thallo.contracts.ids import new_id
from thallo.contracts.models import CostEntry
from thallo.trust import ladder
from thallo.trust import service as svc
from thallo.trust.records import ALL_PROVIDERS, BreakerTrip, CostCap

DEFAULT_CAPS: dict[BreakerScope, Decimal] = {BreakerScope.RUN: Decimal("1.00"), BreakerScope.DAY: Decimal("5.00")}
MAX_CAP = Decimal("10000")
WEEK = timedelta(days=7)


@dataclass(frozen=True)
class GlobalCaps:
    """The operator's caps across every tenant, per UTC day. Set in operator config, not by tenants."""

    day_eur: Decimal = Decimal("50.00")
    per_provider_day_eur: Mapping[str, Decimal] = field(default_factory=dict)


class SpendReader(Protocol):
    def run_total(self, tx: Any, run_ref: str) -> Decimal: ...
    def tenant_day(self, tx: Any, day: date) -> dict[str, Decimal]: ...
    def global_day(self, tx: Any, day: date) -> dict[str, Decimal]: ...


def utc_day(at: datetime) -> date:
    """The day a cost counts against: the UTC calendar day."""
    if at.tzinfo is None:
        raise ValueError("an aware datetime is needed")
    return at.astimezone(UTC).date()


def _sum_by_provider(rows: Iterable[CostEntry], day: date) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in rows:
        if utc_day(r.occurred_at) == day:
            out[r.provider] = out.get(r.provider, Decimal(0)) + r.cost_eur
    return out


class PgSpend:
    """Postgres: totals kept by the cost_ledger trigger (migration 0030); global via app_global_spend."""

    def run_total(self, tx: Any, run_ref: str) -> Decimal:
        row = tx.repo.execute("SELECT coalesce(sum(cost_eur), 0) FROM cost_ledger WHERE run_ref = %s",
                              (run_ref,)).fetchone()
        return Decimal(row[0])

    def tenant_day(self, tx: Any, day: date) -> dict[str, Decimal]:
        rows = tx.repo.execute("SELECT provider, total_eur FROM cost_day_total WHERE day = %s", (day,)).fetchall()
        return {p: Decimal(t) for p, t in rows}

    def global_day(self, tx: Any, day: date) -> dict[str, Decimal]:
        rows = tx.repo.execute("SELECT provider, total_eur FROM app_global_spend(%s)", (day,)).fetchall()
        return {p: Decimal(t) for p, t in rows}


class MemorySpend:
    """In memory: reads cost_ledger rows directly. `all_rows` returns every tenant's rows (the
    in-memory backend's table); without it the global check cannot run, so every call is refused."""

    def __init__(self, all_rows: Callable[[], Iterable[CostEntry]] | None = None) -> None:
        self.all_rows = all_rows

    def run_total(self, tx: Any, run_ref: str) -> Decimal:
        return sum((r.cost_eur for r in tx.list_all(CostEntry, run_ref=run_ref)), Decimal(0))

    def tenant_day(self, tx: Any, day: date) -> dict[str, Decimal]:
        return _sum_by_provider(tx.list_all(CostEntry), day)

    def global_day(self, tx: Any, day: date) -> dict[str, Decimal]:
        if self.all_rows is None:
            raise RuntimeError("no global spend reader")
        return _sum_by_provider(self.all_rows(), day)


def reader_for(tx: Any, memory: MemorySpend | None = None) -> SpendReader:
    if getattr(tx, "repo", None) is not None:
        return PgSpend()
    return memory or MemorySpend()


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    reason: str
    trip: BreakerTrip | None = None
    paused_task_type: bool = False


# ---------------------------------------------------------------- caps

def caps(tx: Any) -> dict[tuple[BreakerScope, str], Decimal]:
    out = {(BreakerScope(c.scope), c.provider): c.cap_eur for c in tx.list_all(CostCap)}
    for scope, cap in DEFAULT_CAPS.items():
        out.setdefault((scope, ALL_PROVIDERS), cap)
    return out


def set_cap(tx: Any, clock: Clock, *, scope: BreakerScope, cap_eur: Decimal, provider: str = ALL_PROVIDERS,
            actor: Actor, verifier: SessionVerifier | None = None) -> CostCap:
    """Lowering a cap is always allowed. Raising one (above the current or default) needs a human."""
    if not isinstance(cap_eur, Decimal) or not cap_eur.is_finite() or cap_eur < 0 or cap_eur > MAX_CAP:
        raise svc.TrustError(f"a cap is a Decimal from 0 to {MAX_CAP}")
    if not provider or len(provider) > 80:
        raise svc.TrustError("a provider name is 1 to 80 characters")
    current = caps(tx).get((scope, provider), caps(tx)[(scope, ALL_PROVIDERS)])
    if cap_eur > current:
        if verifier is None:
            raise svc.TrustError("raising a cap needs a verified person")
        actor = require_human(actor, HumanOnlyAction.SIGN_SPEND, verifier=verifier, tenant_id=tx.tenant_id)
    now = clock.now()
    rows = tx.list_all(CostCap, scope=scope.value, provider=provider)
    if rows:
        tx.update(CostCap, rows[0].id, cap_eur=cap_eur, set_by=actor.id, set_at=now)
        row = tx.get(CostCap, rows[0].id)
    else:
        row = tx.insert(CostCap(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, scope=scope.value,
                                provider=provider, cap_eur=cap_eur, set_by=actor.id, set_at=now))
    svc.audit(tx, actor, "trust.cap_set", "cost_cap", row.id,
              {"scope": scope.value, "provider": provider, "cap_eur": str(cap_eur), "was_eur": str(current)})
    return row


# ---------------------------------------------------------------- the check

def _open_trips(tx: Any, day: date) -> list[BreakerTrip]:
    return [t for t in tx.list_all(BreakerTrip, day=day) if t.reset_at is None]


def _trip(tx: Any, clock: Clock, *, scope: BreakerScope, subject: str, cap: Decimal, spent: Decimal,
          day: date, run_ref: str | None, task_type: str | None) -> tuple[BreakerTrip, bool]:
    now = clock.now()
    trip = tx.insert(BreakerTrip(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, scope=scope.value,
                                 subject=subject, run_ref=run_ref, task_type=task_type, day=day, cap_eur=cap,
                                 spent_eur=spent, tripped_at=now))
    svc.emit(tx, clock, svc.SYSTEM, EventName.BREAKER_TRIPPED,
             BreakerPayload(scope=scope, cap_eur=str(cap), spent_eur=str(spent), subject=subject))
    svc.audit(tx, svc.SYSTEM, "trust.breaker_tripped", "breaker_trip", trip.id,
              {"scope": scope.value, "subject": subject, "cap_eur": str(cap), "spent_eur": str(spent),
               "task_type": task_type})
    paused = False
    if task_type is not None:
        since = now - WEEK
        recent = [t for t in tx.list_all(BreakerTrip, task_type=task_type) if t.tripped_at > since]
        if len(recent) >= ladder.BREAKER_PAUSE_TRIPS:
            paused = svc.pause(tx, clock, task_type, f"cost breaker tripped {len(recent)} times in 7 days")
    return trip, paused


def authorize(tx: Any, clock: Clock, *, run_ref: str, provider: str, estimate_eur: Decimal,
              task_type: str | None = None, global_caps: GlobalCaps | None = None,
              reader: SpendReader | None = None, kill_switch: Callable[[], bool] | None = None) -> Authorization:
    """May this call spend `estimate_eur` now? Never raises: any failure is a refusal."""
    try:
        if not isinstance(estimate_eur, Decimal) or not estimate_eur.is_finite() or estimate_eur < 0:
            return Authorization(False, "the estimate must be a Decimal of at least 0")
        if not run_ref or not provider:
            return Authorization(False, "a run and a provider are needed")
        if global_caps is None:
            return Authorization(False, "no global caps configured; spending is off")
        if kill_switch is not None and kill_switch():
            return Authorization(False, "the global kill switch is on")
        if task_type is not None:
            st, _ = svc.state(tx, clock, task_type)
            if st.paused:
                return Authorization(False, f"task type {task_type} is paused")
        reader = reader or reader_for(tx)
        day = utc_day(clock.now())
        subjects = {"tenant", f"provider:{provider}", "global", f"global:provider:{provider}"}
        for t in _open_trips(tx, day):
            if t.scope == BreakerScope.DAY.value and t.subject in subjects:
                return Authorization(False, f"the {t.subject} breaker tripped today and was not reset", t)
            if t.scope == BreakerScope.RUN.value and t.run_ref == run_ref:
                return Authorization(False, "this run's breaker tripped", t)
        c = caps(tx)
        run_spent = reader.run_total(tx, run_ref)
        mine = reader.tenant_day(tx, day)
        everyone = reader.global_day(tx, day)
        checks: list[tuple[BreakerScope, str, Decimal, Decimal]] = [
            (BreakerScope.RUN, "tenant", c[(BreakerScope.RUN, ALL_PROVIDERS)], run_spent),
            (BreakerScope.DAY, "tenant", c[(BreakerScope.DAY, ALL_PROVIDERS)], sum(mine.values(), Decimal(0))),
            (BreakerScope.DAY, "global", global_caps.day_eur, sum(everyone.values(), Decimal(0))),
        ]
        if (BreakerScope.DAY, provider) in c:
            checks.append((BreakerScope.DAY, f"provider:{provider}", c[(BreakerScope.DAY, provider)],
                           mine.get(provider, Decimal(0))))
        if (BreakerScope.RUN, provider) in c:
            # per-run per-provider: the run's spend is counted whole, the stricter reading
            checks.append((BreakerScope.RUN, f"provider:{provider}", c[(BreakerScope.RUN, provider)], run_spent))
        if provider in global_caps.per_provider_day_eur:
            checks.append((BreakerScope.DAY, f"global:provider:{provider}", global_caps.per_provider_day_eur[provider],
                           everyone.get(provider, Decimal(0))))
        for scope, subject, cap, spent in checks:
            if spent + estimate_eur > cap:
                trip, paused = _trip(tx, clock, scope=scope, subject=subject, cap=cap, spent=spent, day=day,
                                     run_ref=run_ref if scope is BreakerScope.RUN else None, task_type=task_type)
                return Authorization(False, f"{subject} {scope.value} cap of EUR {cap} reached "
                                            f"(spent {spent}, this call {estimate_eur})", trip, paused)
        return Authorization(True, "within every cap")
    except Exception as exc:  # fail closed
        return Authorization(False, f"could not check the caps ({type(exc).__name__}); spending is refused")


def record_cost(tx: Any, clock: Clock, *, capability: Capability, provider: str, cost_eur: Decimal, run_ref: str,
                model: str | None = None, units: Decimal = Decimal(0), subject_kind: str | None = None,
                subject_id: str | None = None) -> CostEntry:
    now = clock.now()
    return tx.insert(CostEntry(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, capability=capability,
                               provider=provider, model=model, units=units, cost_eur=cost_eur, run_ref=run_ref,
                               subject_kind=subject_kind, subject_id=subject_id, occurred_at=now))


def reset_trip(tx: Any, clock: Clock, trip_id: str, *, actor: Actor, verifier: SessionVerifier) -> BreakerTrip:
    human = require_human(actor, HumanOnlyAction.SIGN_SPEND, verifier=verifier, tenant_id=tx.tenant_id)
    trip = tx.get(BreakerTrip, trip_id)
    if trip is None:
        raise svc.TrustError("no such breaker trip")
    if trip.reset_at is not None:
        raise svc.TrustError("already reset")
    tx.update(BreakerTrip, trip.id, reset_at=clock.now(), reset_by=human.id)
    svc.audit(tx, human, "trust.breaker_reset", "breaker_trip", trip.id, {"subject": trip.subject})
    return tx.get(BreakerTrip, trip.id)


__all__ = [
    "DEFAULT_CAPS", "Authorization", "GlobalCaps", "MemorySpend", "PgSpend", "SpendReader", "authorize", "caps",
    "reader_for", "record_cost", "reset_trip", "set_cap", "utc_day",
]
