"""Pipeline mirror (module 17): what happened after the booking, as a projection of rows the lead
desk already writes. `Desk.outcome` is the only writer of `deal` and `outcome`; this module reads.

The guard against sellers not logging: a booking whose start is more than 12 hours past and is
still `booked` is unlogged, counted for the scoreboard and listed for a one-tap reminder.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from thallo.contracts.models import Booking, Deal, Lead, Outcome
from thallo.contracts.states import BookingState, LeadState
from thallo.nurture.booking import kinds_for

STAGES: tuple[tuple[str, frozenset[LeadState]], ...] = (
    ("new", frozenset({LeadState.NEW, LeadState.ACKNOWLEDGED, LeadState.QUALIFYING, LeadState.ROUTED})),
    ("nurturing", frozenset({LeadState.NURTURING})),
    ("contacted", frozenset({LeadState.CONTACTED})),
    ("booked", frozenset({LeadState.BOOKED})),
    ("held", frozenset({LeadState.HELD, LeadState.NO_SHOW})),
    ("won", frozenset({LeadState.WON})),
    ("lost", frozenset({LeadState.LOST})),
    ("parked", frozenset({LeadState.PARKED})),
)
UNLOGGED_AFTER = timedelta(hours=12)


def stage_of(state: LeadState) -> str:
    return next(name for name, states in STAGES if state in states)


def board(tx: Any, clock: Any, play_code: str) -> dict[str, Any]:
    now = clock.now()
    leads = tx.list_all(Lead)
    counts = {name: 0 for name, _ in STAGES}
    for lead in leads:
        counts[stage_of(lead.state)] += 1
    bookings = tx.list_all(Booking)
    kinds = set(kinds_for(play_code))
    booked = [b for b in bookings if b.kind in kinds and b.state is not BookingState.CANCELLED]
    unlogged = sorted((b for b in bookings if b.state is BookingState.BOOKED and b.starts_at < now - UNLOGGED_AFTER),
                      key=lambda b: b.starts_at)
    outcomes = {o.deal_id: o for o in tx.list_all(Outcome)}
    won = [o for o in outcomes.values() if o.kind.value == "won"]
    deals = tx.list_all(Deal)
    return {
        "stages": [{"stage": n, "count": counts[n]} for n, _ in STAGES],
        "booked_conversations": len(booked),
        "booked_this_week": sum(1 for b in booked if b.created_at > now - timedelta(days=7)),
        "unlogged": [{"booking_id": b.id, "lead_id": b.lead_id, "kind": b.kind, "starts_at": b.starts_at.isoformat()}
                     for b in unlogged],
        "unlogged_count": len(unlogged),
        "won_count": len(won),
        "won_amount": str(sum((o.amount or Decimal(0) for o in won), Decimal(0))),
        "lost_count": sum(1 for o in outcomes.values() if o.kind.value == "lost"),
        "deals_without_outcome": sum(1 for d in deals if d.id not in outcomes),
    }


__all__ = ["STAGES", "board", "stage_of"]
