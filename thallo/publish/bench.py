"""The evergreen bench: approved pieces waiting to fill a slot that would otherwise go empty.

All state moves go through the contract machines, `VARIANT` and `PLAN_SLOT`, as compare-and-set
writes (`PublishStore.swap_slot`), so two runners cannot fill one slot twice.

- `bench_variant`: an APPROVED evergreen variant goes to BENCHED (VARIANT APPROVED -> BENCHED).
- `mark_missed`: an OPEN or FILLED slot whose start plus `grace` has passed and that did not go
  out goes to MISSED (event `slot.missed`).
- `fill_from_bench`: a MISSED slot takes the oldest BENCHED variant of the same channel that no
  other slot holds (PLAN_SLOT MISSED -> BENCHED, event `slot.benched`). The variant is then sent
  by the publisher like any other, through `check_publish`: a bench piece whose approval expired
  or whose claims went stale is blocked like anything else, never sent on an old decision.
- `slot_published`: after a successful send, FILLED or BENCHED -> PUBLISHED.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from thallo.contracts.events import EventName, SlotPayload
from thallo.contracts.models import PlanSlot
from thallo.contracts.states import PlanSlotState, VariantState
from thallo.publish.store import PublishStore

GRACE = timedelta(minutes=30)

PS = PlanSlotState


def bench_variant(store: PublishStore, variant_id: str) -> bool:
    v = store.variant(variant_id)
    if v is None or v.is_canary or v.state is not VariantState.APPROVED:
        return False
    return store.move_variant(variant_id, VariantState.BENCHED)


def mark_missed(store: PublishStore, now: datetime, grace: timedelta = GRACE) -> list[PlanSlot]:
    """Slots that should have gone out and did not. A FILLED slot whose variant was published is
    not missed: it is moved to PUBLISHED instead."""
    out: list[PlanSlot] = []
    for state in (PS.OPEN, PS.FILLED):
        for slot in store.slots(state=state):
            if slot.starts_at + grace > now:
                continue
            if state is PS.FILLED and slot.variant_id is not None and _went_out(store, slot.variant_id):
                store.swap_slot(slot.id, PS.FILLED, PS.PUBLISHED)
                continue
            if store.swap_slot(slot.id, state, PS.MISSED):
                store.emit(EventName.SLOT_MISSED, SlotPayload(slot_ref=slot.id, variant_id=slot.variant_id))
                out.append(slot)
    return out


def _went_out(store: PublishStore, variant_id: str) -> bool:
    v = store.variant(variant_id)
    return v is not None and v.state in (VariantState.PUBLISHED, VariantState.READ_BACK_OK,
                                         VariantState.READ_BACK_MISMATCH)


@dataclass(frozen=True)
class Benched:
    slot_id: str
    variant_id: str
    bundle_id: str


def fill_from_bench(store: PublishStore, slot_id: str) -> Benched | None:
    """Fill one MISSED slot from the bench, or None when the bench has nothing for its channel."""
    slot = store.slot(slot_id)
    if slot is None or slot.state is not PS.MISSED or slot.reserved_for_experiment:
        return None
    held = {s.variant_id for s in store.slots() if s.variant_id and s.id != slot.id
            and s.state in (PS.FILLED, PS.BENCHED, PS.PUBLISHED)}
    for v in sorted(store.bench_variants(slot.channel), key=lambda v: v.id):  # oldest first
        if v.id in held:
            continue
        if store.swap_slot(slot.id, PS.MISSED, PS.BENCHED, variant_id=v.id):
            store.emit(EventName.SLOT_BENCHED, SlotPayload(slot_ref=slot.id, variant_id=v.id))
            return Benched(slot.id, v.id, v.bundle_id)
        return None  # someone else moved the slot
    return None


def slot_published(store: PublishStore, slot_id: str) -> bool:
    slot = store.slot(slot_id)
    if slot is None or slot.state not in (PS.FILLED, PS.BENCHED):
        return False
    return store.swap_slot(slot.id, slot.state, PS.PUBLISHED)


__all__ = ["GRACE", "Benched", "bench_variant", "fill_from_bench", "mark_missed", "slot_published"]
