"""Prompt registry and the change gate (module 24). Nothing goes live on a failed eval run.

- `PromptRegistry` versions prompts per tenant on the shared `prompt_version` table: the body is
  hashed with its model, the same body and model is the same version, anything else is the next
  version. A new version starts inactive.
- `gate` decides whether a change (prompt, model or template, keyed by its content hash) may go
  live: it needs, for every client it touches, a passing run bound to that exact change hash and to
  that client's current case set hash. A missing, failed or stale run blocks, and says which.
- `activate` flips a prompt version live only through `gate`. `active` returns only versions that
  were activated, never a merely registered one.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from thallo.contracts.clock import Clock
from thallo.contracts.enums import TextRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import PromptVersion
from thallo.learning.evals import Change
from thallo.learning.records import ChangeKind, EvalRun


class ChangeBlocked(PermissionError):
    pass


@dataclass(frozen=True)
class GateDecision:
    change_sha256: str
    allowed: bool
    reasons: tuple[str, ...]


def gate(change: Change, runs: Sequence[EvalRun], current_case_sets: Mapping[str, str]) -> GateDecision:
    """`current_case_sets` maps each tenant the change touches to its case set hash right now."""
    if not current_case_sets:
        return GateDecision(change.sha256, False, ("no client case set named: nothing was proven",))
    reasons: list[str] = []
    for tenant_id, set_sha in sorted(current_case_sets.items()):
        mine = [r for r in runs if r.tenant_id == tenant_id and r.change_sha256 == change.sha256
                and r.change_kind is change.kind]
        if not mine:
            reasons.append(f"{tenant_id}: no eval run for this change")
            continue
        latest = max(mine, key=lambda r: r.ran_at)
        if latest.case_set_sha256 != set_sha:
            reasons.append(f"{tenant_id}: the latest run is stale, the case set changed since")
        elif not latest.passed:
            reasons.append(f"{tenant_id}: the latest run failed ({latest.failed} failed, {latest.flagged} flagged, "
                           f"{latest.not_run} not graded, {latest.cases} case(s))")
    return GateDecision(change.sha256, not reasons, tuple(reasons))


def prompt_sha(body: str, model_id: str) -> str:
    return hashlib.sha256(f"{model_id}\x00{body}".encode()).hexdigest()


class PromptRegistry:
    """In memory over `prompt_version` rows; the caller persists `rows()`. Bodies are kept beside
    the rows because the shared table stores only their hash."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._rows: dict[str, PromptVersion] = {}
        self._bodies: dict[str, str] = {}

    def rows(self) -> list[PromptVersion]:
        return list(self._rows.values())

    def body(self, version: PromptVersion) -> str:
        return self._bodies[version.id]

    def change(self, version: PromptVersion) -> Change:
        return Change(ChangeKind.PROMPT, f"{version.name}@{version.version}", version.body_sha256)

    def register(self, *, tenant_id: str, name: str, role: TextRole, body: str, model_id: str) -> PromptVersion:
        sha = prompt_sha(body, model_id)
        same = [r for r in self._rows.values() if r.tenant_id == tenant_id and r.name == name]
        for r in same:
            if r.body_sha256 == sha:
                return r
        row = PromptVersion(id=new_id(self.clock), tenant_id=tenant_id, created_at=self.clock.now(), name=name,
                            version=max((r.version for r in same), default=0) + 1, role=role, body_sha256=sha,
                            model_id=model_id)
        self._rows[row.id] = row
        self._bodies[row.id] = body
        return row

    def activate(self, version: PromptVersion, runs: Sequence[EvalRun],
                 current_case_sets: Mapping[str, str]) -> PromptVersion:
        decision = gate(self.change(version), runs, current_case_sets)
        if not decision.allowed:
            raise ChangeBlocked("; ".join(decision.reasons))
        live = version.model_copy(update={"eval_passed": True, "activated_at": self.clock.now()})
        self._rows[live.id] = live
        return live

    def active(self, tenant_id: str, name: str) -> PromptVersion | None:
        live = [r for r in self._rows.values() if r.tenant_id == tenant_id and r.name == name
                and r.activated_at is not None and r.eval_passed]
        return max(live, key=lambda r: (r.activated_at, r.version)) if live else None


__all__ = ["ChangeBlocked", "GateDecision", "PromptRegistry", "gate", "prompt_sha"]
