"""Mirror a week out to the tenant's calendar connection, one way, through the registry.

The built-in week plan is the calendar of record. The mirror sends only planner-made wording
(channel, pillar, stage, metric, format, state): no source text and no draft text. Nothing is
read back: an edit in Notion or Google Calendar changes nothing here. With no calendar
connection the registry serves the manual provider, which mirrors nothing.

Each slot dict carries `slot_id` (a uuid), `title`, `starts_at` and `ends_at` (ISO, UTC) and
`details`. Providers upsert by `slot_id`, so a rerun updates instead of duplicating.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, timedelta
from typing import Any

from thallo.connections.errors import ConnectionsError
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.ids import new_id
from thallo.contracts.models import Pillar, PlanSlot
from thallo.contracts.states import PlanSlotState
from thallo.planner.records import PlannerMirror, PlannerSlot

SLOT_MINUTES = 30


def calendar_slots(tx: Any, week_start: date) -> tuple[dict[str, Any], ...]:
    pillars = {p.id: p.name for p in tx.list_all(Pillar)}
    extra = {s.slot_id: s for s in tx.list_all(PlannerSlot)}
    out = []
    for s in sorted(tx.list_all(PlanSlot, week_start=week_start), key=lambda s: (s.starts_at, s.id)):
        if s.state is PlanSlotState.KILLED:
            continue
        p = extra.get(s.id)
        pillar = pillars.get(s.pillar_id or "", "No pillar")
        bits = [f"Stage: {s.stage.value}", f"State: {s.state.value}"]
        if p is not None:
            bits += [f"Play: {p.play_code}", f"Format: {p.format}", f"Moves: {p.target_metric} by day {p.metric_day}"]
        if s.reserved_for_experiment:
            bits.append("Experiment slot")
        start = s.starts_at.astimezone(UTC)
        out.append({"slot_id": s.id, "title": f"{s.channel.value}: {pillar}", "starts_at": start.isoformat(),
                    "ends_at": (start + timedelta(minutes=SLOT_MINUTES)).isoformat(), "details": "\n".join(bits)})
    return tuple(out)


def mirror_week(tx: Any, registry: ConnectionRegistry, clock: Clock, week_start: date) -> PlannerMirror:
    slots = calendar_slots(tx, week_start)
    digest = hashlib.sha256(json.dumps(slots, sort_keys=True).encode()).hexdigest()[:16]
    req = c.CalendarRequest(tenant_id=tx.tenant_id, slots=slots,
                            idempotency_key=f"calendar:{week_start.isoformat()}:{digest}")
    now = clock.now()
    try:
        served = registry.run(tx.tenant_id, req)
    except ConnectionsError as exc:
        row = PlannerMirror(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, week_start=week_start,
                            provider="unknown", mode="unknown", status="failed", mirrored=0,
                            detail=str(exc)[:500], mirrored_at=now)
        return tx.insert(row)
    out = served.result.output
    mirrored = out.mirrored if isinstance(out, c.CalendarResult) else 0
    return tx.insert(PlannerMirror(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, week_start=week_start,
                                   provider=served.provider, mode=served.mode.value, status="ok", mirrored=mirrored,
                                   mirrored_at=now))


__all__ = ["calendar_slots", "mirror_week"]
