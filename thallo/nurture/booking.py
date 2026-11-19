"""Booking (module 16): offer slots or a link, and record a booked conversation.

A booked conversation, per play (`booked_definition` in docs/plays/plays_draft.json), is a meeting of
one of the play's kinds with a date and time agreed. A link sent is not a booking; a `Booking` row is
written only when a person records the agreed kind and start time. The kinds per play are kept here
because the play file states them in prose (WP-15 owns that file; Sai to confirm the lists).

Booking providers only read: slots or a scheduling link. No lead data goes to the vendor (the
request carries the lead id for the manual task only, and the hosted providers do not send it).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from thallo.contracts import connection as c
from thallo.contracts.actors import Actor, SessionVerifier
from thallo.contracts.models import Booking
from thallo.contracts.states import LeadState
from thallo.leads.desk import Desk, DeskError, _move
from thallo.nurture.engine import Nurture

BOOKED_KINDS: dict[str, tuple[str, ...]] = {
    "A": ("showroom_visit", "test_drive", "callback"),
    "B": ("demo", "trial_kickoff"),
}
MAX_AHEAD = timedelta(days=120)
_LINK = re.compile(r"^https://(cal\.com|app\.cal\.com|calendly\.com)/[A-Za-z0-9._~/%-]{1,200}(\?[A-Za-z0-9=&._%-]{0,200})?$")


def kinds_for(play_code: str) -> tuple[str, ...]:
    return BOOKED_KINDS.get(play_code, ())


def safe_booking_link(link: str | None) -> str | None:
    return link if link and _LINK.match(link) else None


def offer(desk: Desk, lead_id: str, registry: Any, *, kind: str) -> dict[str, Any]:
    """Slots (or a link) to offer the lead. Read only; nothing is booked."""
    lead = desk.lead(lead_id)
    if kind not in kinds_for(desk.setting().play_code):
        raise DeskError("bad_booking", "that is not a booked conversation for this play")
    key = f"booking-offer:{lead.id}:{desk.clock.now():%Y%m%d%H}"
    req = c.BookingRequest(tenant_id=desk.tenant_id, idempotency_key=key, action="slots", lead_id=lead.id, kind=kind)
    served = registry.run(desk.tenant_id, req)
    out = served.result.output
    now = desk.clock.now()
    slots = [s.isoformat() for s in getattr(out, "slots", ()) if now < s <= now + MAX_AHEAD][:12]
    return {"slots": slots, "link": safe_booking_link(getattr(out, "link", None)),
            "task": served.result.task, "provider": served.provider}


def confirm(desk: Desk, lead_id: str, *, kind: str, starts_at: datetime, actor: Actor,
            verifier: SessionVerifier) -> Booking:
    """A person records the agreed date and time. Stops any nurture sequence."""
    if kind not in kinds_for(desk.setting().play_code):
        raise DeskError("bad_booking", "that is not a booked conversation for this play")
    now = desk.clock.now()
    if starts_at.tzinfo is None or not now - timedelta(hours=12) <= starts_at <= now + MAX_AHEAD:
        raise DeskError("bad_booking", "a booking needs the agreed date and time, not long past and within 120 days")
    lead = desk.lead(lead_id)
    Nurture(desk).stop_for_lead(lead.id, "booked")
    if lead.state in (LeadState.ROUTED, LeadState.PARKED):
        _move(desk.tx, lead, LeadState.CONTACTED)  # agreeing a time is a conversation
    return desk.book(lead.id, starts_at=starts_at, kind=kind, actor=actor, verifier=verifier)


__all__ = ["BOOKED_KINDS", "confirm", "kinds_for", "offer", "safe_booking_link"]
