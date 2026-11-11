"""State machines. Code decides what happens next; these tables say what is legal.

Each machine is a set of states plus allowed transitions. `Machine.check` raises
IllegalTransition for anything not listed. Runners (worker/, gate/, leads/) call it
before every state write, and the store keeps the state column as text with a CHECK.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from cortex_operator.contracts.legacy import Status


class IllegalTransition(ValueError):
    pass


@dataclass(frozen=True)
class Machine[S: StrEnum]:
    name: str
    states: type[S]
    initial: S
    transitions: Mapping[S, frozenset[S]]

    def allowed(self, current: S, target: S) -> bool:
        return target in self.transitions.get(current, frozenset())

    def check(self, current: S, target: S) -> S:
        if not self.allowed(current, target):
            raise IllegalTransition(f"{self.name}: {current.value} -> {target.value} is not allowed")
        return target

    def terminal(self) -> frozenset[S]:
        return frozenset(s for s in self.states if not self.transitions.get(s))


def _m[S: StrEnum](name: str, states: type[S], initial: S, edges: dict[S, set[S]]) -> Machine[S]:
    missing = [s for s in edges for t in edges[s] if t not in states]
    if missing:
        raise ValueError(f"{name}: unknown states {missing}")
    return Machine(name, states, initial, {s: frozenset(edges.get(s, set())) for s in states})


# ---------------------------------------------------------------- variant (one piece per channel)

class VariantState(StrEnum):
    DRAFTING = "drafting"
    NEEDS_EVIDENCE = "needs_evidence"
    CHECKING = "checking"
    REVIEW = "review"
    ON_HOLD = "on_hold"
    APPROVED = "approved"
    BENCHED = "benched"  # approved evergreen piece waiting to fill a missed slot
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    READ_BACK_OK = "read_back_ok"
    READ_BACK_MISMATCH = "read_back_mismatch"
    REJECTED = "rejected"
    FAILED = "failed"


V = VariantState
VARIANT = _m("variant", V, V.DRAFTING, {
    V.DRAFTING: {V.NEEDS_EVIDENCE, V.CHECKING},
    V.NEEDS_EVIDENCE: {V.DRAFTING},
    V.CHECKING: {V.DRAFTING, V.REVIEW},  # repair loop (max 3, counted on check_result) or to a human
    V.REVIEW: {V.APPROVED, V.REJECTED, V.DRAFTING, V.ON_HOLD},  # DRAFTING = edited, checks rerun
    V.ON_HOLD: {V.REVIEW, V.REJECTED},
    # A voided or expired approval, or a hash change caught at send time, always reruns the
    # check stack before a person sees it again (BUILD_PLAN 2.6 step 7).
    V.APPROVED: {V.SCHEDULED, V.BENCHED, V.CHECKING},
    V.BENCHED: {V.SCHEDULED, V.CHECKING},
    V.SCHEDULED: {V.PUBLISHED, V.FAILED, V.CHECKING},
    V.PUBLISHED: {V.READ_BACK_OK, V.READ_BACK_MISMATCH},
    V.FAILED: {V.CHECKING, V.SCHEDULED},  # SCHEDULED = retry of the same approved bytes
    V.READ_BACK_OK: set(),
    V.READ_BACK_MISMATCH: set(),
    V.REJECTED: set(),
})

# The original engine's Status maps onto variant states so workflow.py keeps running until ported.
LEGACY_STATUS: dict[Status, VariantState] = {
    Status.IDEA: V.DRAFTING,
    Status.NEEDS_EVIDENCE: V.NEEDS_EVIDENCE,
    Status.REVIEW: V.REVIEW,
    Status.APPROVED: V.APPROVED,
    Status.ON_HOLD: V.ON_HOLD,
    Status.SCHEDULED: V.SCHEDULED,
    Status.PUBLISHED: V.PUBLISHED,
    Status.FAILED: V.FAILED,
}


# ---------------------------------------------------------------- bundle (master plus its variants)

class BundleState(StrEnum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    VOIDED = "voided"  # a byte, claim or rule change after approval
    EXPIRED = "expired"
    REJECTED = "rejected"


B = BundleState
BUNDLE = _m("bundle", B, B.OPEN, {
    B.OPEN: {B.IN_REVIEW},
    B.IN_REVIEW: {B.APPROVED, B.REJECTED, B.OPEN},
    B.APPROVED: {B.VOIDED, B.EXPIRED},
    B.VOIDED: {B.OPEN},  # back through the checks, never straight to review
    B.EXPIRED: {B.OPEN},
    B.REJECTED: set(),
})


# ---------------------------------------------------------------- claim

class ClaimState(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    EXPIRED = "expired"
    VOIDED = "voided"  # value or evidence changed: every approval using it is voided too
    REJECTED = "rejected"


C = ClaimState
CLAIM = _m("claim", C, C.CANDIDATE, {
    C.CANDIDATE: {C.VERIFIED, C.REJECTED},  # VERIFIED only by a HumanActor
    C.VERIFIED: {C.EXPIRED, C.VOIDED},
    C.EXPIRED: {C.VERIFIED, C.REJECTED},
    C.VOIDED: {C.CANDIDATE},
    C.REJECTED: set(),
})


# ---------------------------------------------------------------- idea

class IdeaState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    BRIEFED = "briefed"
    KILLED = "killed"


ID_ = IdeaState
IDEA = _m("idea", ID_, ID_.PROPOSED, {
    ID_.PROPOSED: {ID_.ACCEPTED, ID_.KILLED},
    ID_.ACCEPTED: {ID_.BRIEFED, ID_.KILLED},
    ID_.BRIEFED: set(),
    ID_.KILLED: set(),
})


# ---------------------------------------------------------------- lead

class LeadState(StrEnum):
    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    QUALIFYING = "qualifying"
    ROUTED = "routed"
    NURTURING = "nurturing"
    CONTACTED = "contacted"  # a human replied from their own account
    BOOKED = "booked"
    HELD = "held"
    NO_SHOW = "no_show"
    WON = "won"
    LOST = "lost"
    PARKED = "parked"  # low fit: no outreach, kept for reporting


L = LeadState
LEAD = _m("lead", L, L.NEW, {
    L.NEW: {L.ACKNOWLEDGED, L.QUALIFYING, L.CONTACTED, L.PARKED},  # CONTACTED: no consent basis, personal reply only
    L.ACKNOWLEDGED: {L.QUALIFYING, L.CONTACTED, L.PARKED, L.LOST},
    L.QUALIFYING: {L.ROUTED, L.CONTACTED, L.PARKED, L.LOST},
    L.ROUTED: {L.CONTACTED, L.NURTURING, L.PARKED},
    L.NURTURING: {L.CONTACTED, L.BOOKED, L.PARKED, L.LOST},
    L.CONTACTED: {L.BOOKED, L.NURTURING, L.LOST},
    L.BOOKED: {L.HELD, L.NO_SHOW, L.NURTURING, L.LOST},  # NURTURING/LOST = booking cancelled
    L.HELD: {L.WON, L.LOST},
    L.NO_SHOW: {L.NURTURING, L.BOOKED, L.LOST},
    L.PARKED: {L.CONTACTED},
    L.WON: set(),
    L.LOST: set(),
})


# ---------------------------------------------------------------- spend proposal

class SpendProposalState(StrEnum):
    PROPOSED = "proposed"
    SIGNED = "signed"  # only by a HumanActor
    DECLINED = "declined"
    EXPIRED = "expired"
    APPLIED = "applied"
    VERIFIED = "verified"
    APPLY_FAILED = "apply_failed"


S = SpendProposalState
SPEND_PROPOSAL = _m("spend_proposal", S, S.PROPOSED, {
    S.PROPOSED: {S.SIGNED, S.DECLINED, S.EXPIRED},
    S.SIGNED: {S.APPLIED, S.APPLY_FAILED, S.EXPIRED},  # a signature goes stale with its evidence
    S.APPLIED: {S.VERIFIED, S.APPLY_FAILED},
    S.APPLY_FAILED: {S.APPLIED, S.EXPIRED},  # retry count is capped by the ads desk (WP-22)
    S.DECLINED: set(),
    S.EXPIRED: set(),
    S.VERIFIED: set(),
})


# ---------------------------------------------------------------- rule proposal (learning loop)

class RuleProposalState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"  # live inside its scope
    PROMOTED = "promoted"  # passed the transfer test, brand-wide
    REJECTED = "rejected"
    EXPIRED = "expired"  # unused for 90 days


R = RuleProposalState
RULE_PROPOSAL = _m("rule_proposal", R, R.PROPOSED, {
    R.PROPOSED: {R.ACCEPTED, R.REJECTED},
    R.ACCEPTED: {R.PROMOTED, R.EXPIRED},
    R.PROMOTED: {R.EXPIRED},
    R.REJECTED: set(),
    R.EXPIRED: set(),
})


# ---------------------------------------------------------------- job queue

class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"  # out of attempts, needs a person


J = JobState
JOB = _m("job", J, J.QUEUED, {
    J.QUEUED: {J.RUNNING},
    J.RUNNING: {J.DONE, J.FAILED, J.QUEUED},  # QUEUED = lease expired after a worker crash
    J.FAILED: {J.QUEUED, J.DEAD},
    J.DONE: set(),
    J.DEAD: set(),
})


# ---------------------------------------------------------------- smaller lifecycles

class MessageState(StrEnum):
    DRAFTED = "drafted"
    QUEUED = "queued"  # only template instances under a live template approval
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    DISCARDED = "discarded"


M = MessageState
MESSAGE = _m("message", M, M.DRAFTED, {
    M.DRAFTED: {M.QUEUED, M.SENT, M.DISCARDED},  # SENT directly = a human sent it from their own app
    M.QUEUED: {M.SENT, M.FAILED, M.DISCARDED},
    M.SENT: {M.DELIVERED, M.FAILED},
    M.FAILED: {M.QUEUED, M.DISCARDED},
    M.DELIVERED: set(),
    M.DISCARDED: set(),
})


class BookingState(StrEnum):
    BOOKED = "booked"
    HELD = "held"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"


BK = BookingState
BOOKING = _m("booking", BK, BK.BOOKED, {
    BK.BOOKED: {BK.HELD, BK.NO_SHOW, BK.CANCELLED},
    BK.HELD: set(),
    BK.NO_SHOW: set(),
    BK.CANCELLED: set(),
})


class AdState(StrEnum):
    CREATED_PAUSED = "created_paused"  # every ad starts paused
    ACTIVE = "active"  # only after a signed spend proposal
    PAUSED = "paused"
    ARCHIVED = "archived"


A = AdState
AD = _m("ad", A, A.CREATED_PAUSED, {
    A.CREATED_PAUSED: {A.ACTIVE, A.ARCHIVED},
    A.ACTIVE: {A.PAUSED},  # the only unasked automatic action
    A.PAUSED: {A.ACTIVE, A.ARCHIVED},
    A.ARCHIVED: set(),
})


MACHINES: dict[str, Machine] = {m.name: m for m in (
    VARIANT, BUNDLE, CLAIM, IDEA, LEAD, SPEND_PROPOSAL, RULE_PROPOSAL, JOB, MESSAGE, BOOKING, AD,
)}
