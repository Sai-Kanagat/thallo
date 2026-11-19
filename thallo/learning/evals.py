"""The eval harness (module 24, QUALITY_BAR 8). Proves a prompt, model or template change is no
worse on every client's own cases before it goes live. Time from a Clock.

A run takes one change, one client's case set and a `generate` callable (the change under test
turning a case's brief or draft into a piece). For each case:

- Code checks, the hard gate. Any failure fails the case:
  `empty` (no output), `rule:<id>` (a live voice rule in the case's scope is broken),
  `regression` (the output brings back wording the human removed from that case's draft),
  `unslotted_number` (a digit outside a claim slot: the model never types a number).
- Model graders, through the connection registry with role GRADE. The piece is quoted in
  `untrusted_data`, never in the instructions. A grader's verdict is "pass" or "flag"; a missing,
  pending or malformed reply is "not_run", never a pass. Graders in `dropped` (they lost their vote
  in `thallo.checks.agreement`) are not asked.

The run passes only when the set is not empty, no case failed, no grader flagged and every voting
grader ran on every case. It is bound to the change hash and the case set hash: a new case makes
an old pass stale.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any

from thallo.connections.registry import ConnectionRegistry
from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import RuleScope, TextRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import VoiceRule
from thallo.learning.diffs import SLOT, classify
from thallo.learning.records import ChangeKind, EvalRun, RegressionCase
from thallo.learning.rules import parse_rule

GRADER_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["verdict", "reason"],
    "properties": {"verdict": {"enum": ["pass", "flag"]}, "reason": {"type": "string"}},
}


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    name: str
    sha256: str

    @classmethod
    def of(cls, kind: ChangeKind, name: str, *parts: str) -> Change:
        return cls(kind, name, hashlib.sha256("\x00".join(parts).encode()).hexdigest())


@dataclass(frozen=True)
class ModelGrader:
    name: str
    instructions: str
    prompt_sha256: str  # the grader prompt version, from the prompt registry


Generate = Callable[[RegressionCase], str]


def case_set_sha(cases: Sequence[RegressionCase]) -> str:
    body = sorted((x.id, hashlib.sha256(x.final.encode()).hexdigest(), x.draft or "") for x in cases)
    return hashlib.sha256(json.dumps(body).encode()).hexdigest()


def _rule_applies(rule: VoiceRule, case: RegressionCase) -> bool:
    if rule.scope is RuleScope.BRAND:
        return True
    if rule.scope is RuleScope.CHANNEL:
        return case.channel is not None and case.channel.value == rule.scope_ref
    return case.scope is RuleScope.PILLAR and case.scope_ref == rule.scope_ref


def removed_wording(case: RegressionCase) -> list[str]:
    if not case.draft:
        return []
    final = case.final.lower()
    return [p for p in classify(case.draft, case.final).removed
            if p not in final and (len(p.split()) >= 2 or len(p) >= 4)]


def code_failures(case: RegressionCase, output: str, rules: Sequence[VoiceRule]) -> list[str]:
    if not output.strip():
        return ["empty"]
    out: list[str] = []
    for r in rules:
        if _rule_applies(r, case) and parse_rule(r.body).broken_by(output):
            out.append(f"rule:{r.id}")
    low = output.lower()
    if any(re.search(r"(?<![\w'])" + re.escape(p) + r"(?![\w'])", low) for p in removed_wording(case)):
        out.append("regression")
    if re.search(r"\d", SLOT.sub("", output)):
        out.append("unslotted_number")
    return out


def ask_grader(registry: ConnectionRegistry, tenant_id: str, grader: ModelGrader, change: Change,
               case: RegressionCase, output: str) -> tuple[str, str]:
    request = c.TextRequest(
        tenant_id=tenant_id, role=TextRole.GRADE, instructions=grader.instructions,
        untrusted_data=(output, case.final), output_schema=GRADER_SCHEMA, language=case.language,
        idempotency_key=f"eval:{change.sha256[:16]}:{grader.prompt_sha256[:16]}:{case.id}")
    try:
        served = registry.run(tenant_id, request)
    except Exception as exc:  # noqa: BLE001 - any failure to grade is "not run", never a pass
        return "not_run", type(exc).__name__
    if served.result.status != "done" or not isinstance(served.result.output, c.TextResult):
        return "not_run", "no reply"
    data = served.result.output.data
    if data is None:
        try:
            data = json.loads(served.result.output.text)
        except ValueError:
            return "not_run", "reply was not JSON"
    verdict = data.get("verdict") if isinstance(data, dict) else None
    if verdict not in ("pass", "flag"):
        return "not_run", "reply had no verdict"
    return verdict, str(data.get("reason", ""))[:500]


def run_eval(*, tenant_id: str, change: Change, cases: Sequence[RegressionCase], generate: Generate,
             rules: Sequence[VoiceRule], clock: Clock, graders: Sequence[ModelGrader] = (),
             registry: ConnectionRegistry | None = None, dropped: Collection[str] = ()) -> EvalRun:
    cases = [x for x in cases if x.tenant_id == tenant_id]
    rules = [r for r in rules if r.tenant_id == tenant_id]
    voting = [g for g in graders if g.name not in dropped]
    if voting and registry is None:
        raise ValueError("model graders need the connection registry")
    detail: list[dict[str, Any]] = []
    failed = flagged = not_run = 0
    for case in cases:
        output = generate(case)
        fails = code_failures(case, output, rules)
        grades: dict[str, dict[str, str]] = {}
        for g in voting:
            verdict, reason = ask_grader(registry, tenant_id, g, change, case, output)  # type: ignore[arg-type]
            grades[g.name] = {"verdict": verdict, "reason": reason}
        failed += bool(fails)
        flagged += any(v["verdict"] == "flag" for v in grades.values())
        not_run += any(v["verdict"] == "not_run" for v in grades.values())
        detail.append({"case_id": case.id, "failures": fails, "graders": grades,
                       "output_sha256": hashlib.sha256(output.encode()).hexdigest()})
    passed = bool(cases) and failed == 0 and flagged == 0 and not_run == 0
    return EvalRun(id=new_id(clock), tenant_id=tenant_id, created_at=clock.now(), change_kind=change.kind,
                   change_name=change.name, change_sha256=change.sha256, case_set_sha256=case_set_sha(cases),
                   cases=len(cases), failed=failed, flagged=flagged, not_run=not_run, passed=passed,
                   detail=detail, ran_at=clock.now())


__all__ = ["GRADER_SCHEMA", "Change", "Generate", "ModelGrader", "ask_grader", "case_set_sha", "code_failures",
           "removed_wording", "run_eval"]
