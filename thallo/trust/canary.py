"""Reviewer canaries (BUILD_PLAN 2.8): from L2 on, about one item in twenty is a planted flaw.

A canary is a copy of a real variant with one flaw (a number swapped for a wrong one, a banned
phrase, or an off-brand image chosen by the caller), marked `is_canary` with its `canary_flaw`.
The gate refuses any bundle that holds one, whatever else holds (`gate.decision`, BlockReason
CANARY), so the planner puts each canary in a bundle of its own.

When the reviewer rejects it, that is a catch. When they approve it, they are told at once and
the task type drops a level. Canaries never count as items: a caught canary is not a defect and
a missed one is not a clean item.

Which items become canaries is deterministic: no random number, so a run can be replayed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from thallo.contracts.actors import Actor
from thallo.contracts.clock import Clock
from thallo.contracts.events import CanaryPayload, EventName
from thallo.contracts.ids import new_id
from thallo.contracts.models import AutonomyState, Variant
from thallo.contracts.states import VariantState
from thallo.trust import ladder
from thallo.trust import service as svc
from thallo.trust.records import CanaryFlaw, CanaryState, TrustCanary

BANNED_PHRASES = ("Guaranteed results, zero risk.", "The best in the world, no question.")
MISSED_NOTICE = ("That was a planted canary: {flaw}. It will never be published. This task type "
                 "drops one level, and the catch rate on the trust screen counts it.")
FLAW_WORDS = {
    CanaryFlaw.SWAPPED_CLAIM: "a number was swapped for a wrong one",
    CanaryFlaw.BANNED_PHRASE: "a banned phrase was added",
    CanaryFlaw.OFF_BRAND_IMAGE: "the image was off brand",
}
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class CanaryError(svc.TrustError):
    pass


@dataclass(frozen=True)
class Resolution:
    canary: TrustCanary
    caught: bool
    notice: str | None  # shown to the reviewer at once when they approved a canary
    changes: tuple[svc.LevelChange, ...]


def _offset(tenant_id: str, task_type: str) -> int:
    return int(hashlib.sha256(f"{tenant_id}\n{task_type}".encode()).hexdigest()[:8], 16) % ladder.CANARY_RATE[1]


def due(tx: Any, clock: Clock, task_type: str) -> bool:
    """Should the next item of this task type be a canary? Only from L2 on, and never when paused."""
    st, _ = svc.state(tx, clock, task_type)
    if st.level < ladder.L2 or st.paused:
        return False
    return ladder.plant_canary_now(st.window_items + st.canaries_shown, _offset(tx.tenant_id, st.task_type))


def swap_number(text: str) -> str:
    """Change the first number in the text to a wrong one (a fixed rule, not a random draw)."""
    m = _NUMBER.search(text)
    if m is None:
        raise CanaryError("no number to swap in this text")
    raw = m.group(0)
    sep = "," if "," in raw else "."
    whole, _, frac = raw.replace(",", ".").partition(".")
    wrong = str(int(whole) * 3 + 7)
    wrong = f"{wrong}{sep}{frac}" if frac else wrong
    return text[:m.start()] + wrong + text[m.end():]


def flawed_text(text: str, flaw: CanaryFlaw, *, banned_phrase: str | None = None) -> str:
    if flaw is CanaryFlaw.SWAPPED_CLAIM:
        return swap_number(text)
    if flaw is CanaryFlaw.BANNED_PHRASE:
        phrase = banned_phrase or BANNED_PHRASES[len(text) % len(BANNED_PHRASES)]
        return f"{text.rstrip()} {phrase}"
    return text  # an off-brand image: the caller links the wrong image; the words stay


def plant(tx: Any, clock: Clock, *, source_variant_id: str, bundle_id: str, flaw: CanaryFlaw,
          banned_phrase: str | None = None) -> tuple[Variant, TrustCanary]:
    """Copy a variant into `bundle_id` with one flaw, marked as a canary, in review."""
    src = tx.get(Variant, source_variant_id)
    if src is None:
        raise CanaryError("no such variant")
    if src.is_canary:
        raise CanaryError("a canary is never copied into another canary")
    st, _ = svc.state(tx, clock, src.task_type)
    svc._lock(tx, st)
    st = tx.get(AutonomyState, st.id)
    if st.level < ladder.L2:
        raise CanaryError("canaries are planted from L2 on")
    if flaw is CanaryFlaw.SWAPPED_CLAIM:
        if not src.rendered_text:
            raise CanaryError("a swapped claim needs the filled text; this variant has none yet")
        text = rendered = flawed_text(src.rendered_text, flaw)  # the wrong number is literal: no slot fills it
    else:
        text = flawed_text(src.text, flaw, banned_phrase=banned_phrase)
        rendered = flawed_text(src.rendered_text, flaw, banned_phrase=banned_phrase) if src.rendered_text else None
    now = clock.now()
    variant = tx.insert(src.model_copy(update={
        "id": new_id(clock), "created_at": now, "bundle_id": bundle_id, "state": VariantState.REVIEW, "version": 1,
        "text": text, "rendered_text": rendered, "content_sha256": None, "scheduled_for": None,
        "is_canary": True, "canary_flaw": flaw.value}))
    canary = tx.insert(TrustCanary(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, task_type=st.task_type,
                                   variant_id=variant.id, flaw=flaw, planted_at=now))
    tx.update(AutonomyState, st.id, canaries_shown=st.canaries_shown + 1)
    svc.emit(tx, clock, svc.SYSTEM, EventName.CANARY_PLANTED, CanaryPayload(variant_id=variant.id,
                                                                           task_type=st.task_type))
    svc.audit(tx, svc.SYSTEM, "trust.canary_planted", "variant", variant.id,
              {"task_type": st.task_type, "flaw": flaw.value})
    return variant, canary


def resolve(tx: Any, clock: Clock, *, variant_id: str, approved: bool, reviewer: Actor) -> Resolution:
    """The reviewer's call on a canary. Approving it is a miss: the task type drops one level."""
    rows = tx.list_all(TrustCanary, variant_id=variant_id)
    if not rows:
        raise CanaryError("not a canary")
    canary = rows[0]
    if canary.state is not CanaryState.PLANTED:
        raise CanaryError("this canary is already resolved")
    st, _ = svc.state(tx, clock, canary.task_type)
    svc._lock(tx, st)
    st = tx.get(AutonomyState, st.id)
    now = clock.now()
    new_state = CanaryState.MISSED if approved else CanaryState.CAUGHT
    reviewer_id = reviewer.id if getattr(reviewer, "kind", None) == "human" else None
    tx.update(TrustCanary, canary.id, state=new_state, resolved_at=now, resolved_by=reviewer_id)
    tx.update(Variant, variant_id, state=VariantState.REJECTED)  # a canary never moves on
    changes: tuple[svc.LevelChange, ...] = ()
    if approved:
        svc.emit(tx, clock, svc.SYSTEM, EventName.CANARY_MISSED, CanaryPayload(variant_id=variant_id,
                                                                              task_type=st.task_type))
        if canary.planted_at >= st.changed_at:
            changes = svc._demote(tx, clock, st, ladder.down_one(st.level), "a reviewer canary was approved")
    else:
        if canary.planted_at >= st.changed_at:
            tx.update(AutonomyState, st.id, canaries_caught=st.canaries_caught + 1)
        svc.emit(tx, clock, svc.SYSTEM, EventName.CANARY_CAUGHT, CanaryPayload(variant_id=variant_id,
                                                                              task_type=st.task_type))
    svc.audit(tx, reviewer, "trust.canary_resolved", "variant", variant_id,
              {"task_type": st.task_type, "caught": not approved})
    notice = MISSED_NOTICE.format(flaw=FLAW_WORDS[canary.flaw]) if approved else None
    return Resolution(tx.get(TrustCanary, canary.id), not approved, notice, changes)


__all__ = ["BANNED_PHRASES", "CanaryError", "Resolution", "due", "flawed_text", "plant", "resolve", "swap_number"]
