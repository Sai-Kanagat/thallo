"""Check a play against the tenant's region policy before it is saved (module 39's guard).

`region_policy.rules` is filled from a lawyer's answers. No other package reads it yet, so this
file defines the smallest vocabulary a play can contradict:

    blocked_channels       [channel, ...]   channels the play may not use in this region
    blocked_formats        [format, ...]    formats the play may not use
    blocked_question_keys  [key, ...]       data a qualification question may not ask for
    max_channels_live      int              cap on channels live at once

Other keys are reported as "not checked by the play editor", never read as a pass. With no
policy row on file the result is NO_POLICY and the editor refuses to save: fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from thallo.contracts.models import RegionPolicy
from thallo.growth.plays import PlaySpec

KNOWN_RULES = frozenset({"blocked_channels", "blocked_formats", "blocked_question_keys", "max_channels_live"})


class PolicyStatus(StrEnum):
    OK = "ok"
    CONTRADICTS = "contradicts"
    NO_POLICY = "no_policy"


@dataclass(frozen=True)
class PolicyResult:
    status: PolicyStatus
    region_code: str
    version: int | None = None
    problems: list[str] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {"status": self.status.value, "region_code": self.region_code, "version": self.version,
                "problems": self.problems, "not_checked": self.not_checked}


def current_policy(rows: list[RegionPolicy], region_code: str, now: datetime) -> RegionPolicy | None:
    live = [r for r in rows if r.region_code == region_code and r.effective_from <= now]
    return max(live, key=lambda r: r.version, default=None)


def _strings(v: object) -> set[str]:
    if isinstance(v, (list, tuple)):
        return {str(x) for x in v}
    return {str(v)} if v is not None else set()


def check_play(spec: PlaySpec, policy: RegionPolicy | None, region_code: str) -> PolicyResult:
    if policy is None:
        return PolicyResult(PolicyStatus.NO_POLICY, region_code,
                            problems=[f"No region policy on file for {region_code}, so this play is not validated."])
    rules = policy.rules or {}
    problems: list[str] = []
    for ch in sorted(_strings(rules.get("blocked_channels")) & {c.value for c in spec.channels}):
        problems.append(f"The policy for {region_code} blocks the channel {ch}.")
    for f in sorted(_strings(rules.get("blocked_formats")) & set(spec.formats)):
        problems.append(f"The policy for {region_code} blocks the format {f}.")
    for k in sorted(_strings(rules.get("blocked_question_keys")) & {q.key for q in spec.qualification_questions}):
        problems.append(f"The policy for {region_code} does not allow asking for {k}.")
    cap = rules.get("max_channels_live")
    if cap is not None:
        try:
            if spec.weekly_volume.max_channels_live > int(cap):
                problems.append(f"The policy for {region_code} allows at most {int(cap)} channels live at once.")
        except (TypeError, ValueError):
            problems.append(f"The policy's max_channels_live ({cap!r}) is not a number, so it cannot be checked.")
    not_checked = sorted(set(rules) - KNOWN_RULES)
    status = PolicyStatus.CONTRADICTS if problems else PolicyStatus.OK
    return PolicyResult(status, region_code, policy.version, problems, not_checked)


__all__ = ["KNOWN_RULES", "PolicyResult", "PolicyStatus", "check_play", "current_policy"]
