"""Per channel rules the publisher checks before it claims a send, and the pre-flight that runs
the same rules plus the gate three days ahead, so a person has time to fix what would block.

Only limits this repository already relied on are listed (from the original engine): LinkedIn
3,000 characters, Instagram 2,200 and a required image. Other channels have no limit here yet;
an unknown limit is not checked, never guessed.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from thallo.contracts.enums import Channel
from thallo.contracts.models import ChannelAccount, PlanSlot
from thallo.contracts.states import PlanSlotState
from thallo.gate.decision import GateDecision

TEXT_LIMITS: dict[Channel, int] = {
    Channel.LINKEDIN_PERSONAL: 3000, Channel.LINKEDIN_COMPANY: 3000, Channel.INSTAGRAM: 2200,
}
NEEDS_MEDIA = frozenset({Channel.INSTAGRAM})
PREFLIGHT_AHEAD = timedelta(days=3)


def channel_problems(channel: str, text: str, media_count: int) -> list[str]:
    try:
        ch = Channel(channel)
    except ValueError:
        return [f"unknown channel {channel!r}"]
    out: list[str] = []
    if not text.strip() and not media_count:
        out.append("nothing to post: no text and no media")
    limit = TEXT_LIMITS.get(ch)
    if limit is not None and len(text) > limit:
        out.append(f"text is {len(text)} characters, over the {ch.value} limit of {limit}")
    if ch in NEEDS_MEDIA and media_count == 0:
        out.append(f"{ch.value} needs an image or video")
    return out


@dataclass(frozen=True)
class SlotCheck:
    slot_id: str
    variant_id: str | None
    starts_at: datetime
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


Decide = Callable[[PlanSlot], GateDecision]


def preflight(slots: list[PlanSlot], *, now: datetime, decide: Decide,
              account: Callable[[PlanSlot], ChannelAccount | None],
              approval_expires: Callable[[PlanSlot], datetime | None]) -> list[SlotCheck]:
    """Every filled slot starting within `PREFLIGHT_AHEAD`: the gate decision as it stands now
    (read only, nothing is sent or written), the channel rules on the payload, a channel account
    for the slot's channel whose token outlives the slot, and an approval that outlives the slot.
    Returns one `SlotCheck` per slot; a person fixes what it names."""
    out: list[SlotCheck] = []
    for slot in sorted(slots, key=lambda s: s.starts_at):
        if slot.state is not PlanSlotState.FILLED or not (now < slot.starts_at <= now + PREFLIGHT_AHEAD):
            continue
        problems: list[str] = []
        if slot.variant_id is None:
            problems.append("the slot has no variant")
        else:
            decision = decide(slot)
            if not decision.allowed:
                problems += [f"gate: {b.reason.value}: {b.detail}" for b in decision.blocks]
            elif decision.payload is not None:
                if decision.payload.channel != slot.channel.value:
                    problems.append("the variant is for another channel than the slot")
                problems += channel_problems(decision.payload.channel, decision.payload.text,
                                             len(decision.payload.media))
            expires = approval_expires(slot)
            if expires is not None and expires <= slot.starts_at:
                problems.append("the approval expires before the slot starts; approve again")
        acct = account(slot)
        if acct is None:
            problems.append(f"no channel account for {slot.channel.value}")
        elif acct.token_expires_at is not None and acct.token_expires_at <= slot.starts_at:
            problems.append(f"the {slot.channel.value} account token expires before the slot; reconnect it")
        out.append(SlotCheck(slot.id, slot.variant_id, slot.starts_at, tuple(problems)))
    return out


__all__ = ["NEEDS_MEDIA", "PREFLIGHT_AHEAD", "TEXT_LIMITS", "SlotCheck", "channel_problems", "preflight"]
