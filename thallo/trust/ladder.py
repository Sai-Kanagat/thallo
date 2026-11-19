"""The trust ladder as plain rules (BUILD_PLAN 2.8). No storage, no clock: counts in, answers out.

Autonomy is tracked per tenant and task type. Unlocking is never automatic: `eligibility` says
whether the measured evidence meets the stated bound, and only a named human may then raise
the level, one step at a time (`service.raise_level`). Demotion is automatic and immediate.

Whatever the level, no human-only action (contracts `HumanOnlyAction`) is ever done by the
system alone: `system_may_do_alone` answers False for every one of them at every level.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import comb

from thallo.contracts.enums import AutonomyLevel, HumanOnlyAction

L0, L1, L2, L3, L4 = (AutonomyLevel.L0_SHADOW, AutonomyLevel.L1_FULL_REVIEW, AutonomyLevel.L2_BUNDLE_REVIEW,
                      AutonomyLevel.L3_EXPRESS_LIST, AutonomyLevel.L4_TEMPLATE_INSTANCES)

CALIBRATION_BATCH = 20
RECENT_DEFECT_WINDOW = 20  # two defects in the last 20 items: down one level
RECENT_DEFECT_LIMIT = 2
VERSION_REEARN = 30  # after a model or prompt version change, 30 clean items before any raise
MIN_CANARIES = 5  # below this a catch rate says nothing; at five it is still thin
CANARY_RATE = 1, 20  # about one item in twenty is a planted flaw, from L2 on
BREAKER_PAUSE_TRIPS = 2  # breaker trips in a rolling 7 days that pause a task type
CONFIDENCE = 0.95


@dataclass(frozen=True)
class UnlockRule:
    target: AutonomyLevel
    items: int  # the window: the most recent this many items since the last level change
    max_defects: int | None
    canary_catch_pct: int | None = None
    needs_calibration: bool = False
    needs_voice_rules: bool = False
    needs_template: bool = False


RULES: dict[AutonomyLevel, UnlockRule] = {
    L1: UnlockRule(L1, CALIBRATION_BATCH, None, needs_calibration=True, needs_voice_rules=True),
    L2: UnlockRule(L2, 50, 2),
    L3: UnlockRule(L3, 100, 1, canary_catch_pct=80),
    L4: UnlockRule(L4, 200, 3, needs_template=True),
}

# What the system does alone at each level (2.8). Text for the screen; the publisher and the
# gate enforce the rest whatever this says.
ALONE: dict[AutonomyLevel, str] = {
    L0: "Drafts only. You see the draft beside your own version.",
    L1: "Drafts, checks and renders. You see every item as a full card with every check open.",
    L2: "Groups variants, sorts and collapses clean checks. You see one card per bundle, flags open.",
    L3: "Presents clean bundles as one list with approve all except. You still approve exact hashes.",
    L4: "Sends instances of a template you signed word for word. You see a 1 in 20 sample after.",
}


def system_may_do_alone(action: HumanOnlyAction, level: AutonomyLevel) -> bool:
    """No level unlocks a human-only action. This is the whole rule, and it does not change."""
    del action, level
    return False


# ---------------------------------------------------------------- the bound

def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def defect_upper_bound(defects: int, items: int, confidence: float = CONFIDENCE) -> float:
    """Exact one-sided (Clopper-Pearson) upper bound on the true defect rate: the p at which
    seeing `defects` or fewer in `items` has probability 1 - confidence."""
    if items <= 0:
        return 1.0
    if not 0 <= defects <= items:
        raise ValueError("defects must be between 0 and items")
    if defects == items:
        return 1.0
    lo, hi = defects / items, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _binom_cdf(defects, items, mid) > 1 - confidence:
            lo = mid
        else:
            hi = mid
    return hi


def bound_pct(defects: int, items: int) -> str:
    """The bound as a percentage string with one decimal, for payloads and screens."""
    return f"{defect_upper_bound(defects, items) * 100:.1f}"


# ---------------------------------------------------------------- unlock

@dataclass(frozen=True)
class Evidence:
    level: AutonomyLevel
    items: int  # items in the window (most recent, since the last level change), canaries excluded
    defects: int
    escaped: int
    hard_rule_hits: int
    canaries_shown: int = 0
    canaries_caught: int = 0
    calibration_done: bool = False
    voice_rules_signed: bool = False
    template_signed: bool = False
    version_reearn_needed: bool = False
    clean_since_version: int = 0
    paused: bool = False


@dataclass(frozen=True)
class Eligibility:
    current: AutonomyLevel
    target: AutonomyLevel | None
    ok: bool
    reasons: tuple[str, ...]
    bound_pct: str | None  # the 95% upper bound on the defect rate over the window
    canary_note: str | None


def canary_note(shown: int, caught: int) -> str:
    if shown == 0:
        return "No canaries shown yet, so the review has not been measured."
    rate = caught * 100 // shown
    thin = " That is thin evidence." if shown < 20 else ""
    return f"{caught} of {shown} canaries caught ({rate}%).{thin}"


def eligibility(ev: Evidence) -> Eligibility:
    """Can a human raise this task type one level now? Lists every unmet condition."""
    note = canary_note(ev.canaries_shown, ev.canaries_caught) if ev.level >= L2 else None
    if ev.level == L4:
        return Eligibility(ev.level, None, False, ("Already at the top level.",), None, note)
    target = AutonomyLevel(ev.level + 1)
    rule = RULES[target]
    reasons: list[str] = []
    if ev.paused:
        reasons.append("This task type is paused.")
    if ev.items < rule.items:
        reasons.append(f"{ev.items} of {rule.items} items reviewed at this level.")
    if rule.max_defects is not None and ev.defects > rule.max_defects:
        reasons.append(f"{ev.defects} defects in the last {rule.items}; at most {rule.max_defects} allowed.")
    if rule.max_defects is not None and ev.escaped:
        reasons.append(f"{ev.escaped} defects escaped review; none allowed.")
    if ev.hard_rule_hits:
        reasons.append("A hard-rule hit reached approval.")
    if rule.needs_calibration and not ev.calibration_done:
        reasons.append(f"The calibration batch of {CALIBRATION_BATCH} is not done.")
    if rule.needs_voice_rules and not ev.voice_rules_signed:
        reasons.append("Voice rules v1 are not signed.")
    if rule.needs_template and not ev.template_signed:
        reasons.append("No signed template.")
    if rule.canary_catch_pct is not None:
        if ev.canaries_shown < MIN_CANARIES:
            reasons.append(f"{ev.canaries_shown} canaries shown; at least {MIN_CANARIES} needed.")
        elif ev.canaries_caught * 100 < rule.canary_catch_pct * ev.canaries_shown:
            reasons.append(f"Canary catch rate under {rule.canary_catch_pct}%.")
    if ev.version_reearn_needed and ev.clean_since_version < VERSION_REEARN:
        reasons.append(f"Model or prompt changed: {ev.clean_since_version} of {VERSION_REEARN} clean items since.")
    bound = bound_pct(ev.defects, ev.items) if rule.max_defects is not None and ev.items else None
    return Eligibility(ev.level, target, not reasons, tuple(reasons), bound, note)


# ---------------------------------------------------------------- demotion

def down_one(level: AutonomyLevel) -> AutonomyLevel:
    return AutonomyLevel(max(0, level - 1))


def to_l1(level: AutonomyLevel) -> AutonomyLevel:
    """Back to full review. A task type still in shadow stays there."""
    return min(level, L1)


def plant_canary_now(seq: int, offset: int) -> bool:
    """Deterministic: the item at position `seq` (0-based, per task type) is a canary when it
    lands on the task type's slot in each run of twenty. No random number is drawn."""
    one, every = CANARY_RATE
    del one
    return (seq + offset) % every == every - 1


__all__ = [
    "ALONE", "BREAKER_PAUSE_TRIPS", "CALIBRATION_BATCH", "CANARY_RATE", "MIN_CANARIES", "RECENT_DEFECT_LIMIT",
    "RECENT_DEFECT_WINDOW", "RULES", "VERSION_REEARN", "Eligibility", "Evidence", "UnlockRule", "bound_pct",
    "canary_note", "defect_upper_bound", "down_one", "eligibility", "plant_canary_now", "system_may_do_alone",
    "to_l1",
]
