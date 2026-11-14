"""Copy production (BUILD_PLAN module 8, section 2.7 "Copy", WAVE2_ADDENDUM WP-06).

    outcome = produce_copy(registry=..., tenant=..., brief=..., claims=library(repo, tenant.id),
                           sources=[...], channel=Channel.INSTAGRAM, rule_version=1, clock=clock)
    with db.tenant(tenant.id) as repo:
        write_outcome(repo, outcome)

What it does, in order:
1. Source or silence: no source material, no draft and no call.
2. Three candidates, each from its own angle, written by the DRAFT role through the registry (the
   model is the connection row's). Source material and every free-text brief field go in
   `untrusted_data`, never in the instructions.
3. Every candidate goes through `truth.build`. A `CompileFailed` (or a fill failure) is repaired by
   feeding the draft and the findings back as untrusted data: at most three repair loops, so
   attempts 1 to 4, counted on `check_result.attempt`.
4. The slop catalogue runs on every candidate that built, before selection.
5. Selection: an optional grader score first, then fewer open flags, fewer slop hits, within the
   length limit, fewer attempts, earlier candidate. The reason is stored on `asset`.
6. A candidate that built goes to the check stack (WP-07) as a variant in CHECKING. If none built,
   the best failed one goes to a person as a variant left in DRAFTING (never REVIEW, so a FAIL can
   never be approved by accident), with the failing check named in `asset.selection_reason`.

FAIL rows block, FLAG rows are kept with verdict FLAG, never dropped or downgraded. When a later
attempt no longer raises a check an earlier one raised, a PASS row at the later attempt closes it,
so "latest attempt per check" (the gate's rule for open flags) always describes the current text.

Cost: each call's cost goes to `cost_ledger`. The piece has a cap (USD 0.50 short, USD 3 long,
converted at `USD_TO_EUR`); before every call the first connection's estimate is checked against
what is left, and the run stops before a call that would go over. The registry gets the remainder
as `cost_cap_eur`, so it also refuses on its own.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from thallo.connections.errors import ConnectionsError
from thallo.connections.registry import ConnectionRegistry, Served
from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, Channel, CheckLayer, CheckVerdict, ConnectionMode, TextRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import Asset, Brief, Bundle, CheckResult, Claim, CostEntry, Tenant, Variant, VariantClaim
from thallo.contracts.states import VARIANT, VariantState
from thallo.copy import prompts
from thallo.copy.slop import SlopHit, find_slop
from thallo.providers.text.base import USD_TO_EUR
from thallo.truth import Built, CompileFailed, CompileResult, FillError, build, to_check_results

CANDIDATES = 3
MAX_REPAIR_LOOPS = 3
PieceLength = Literal["short", "long"]
COST_CAP_USD: dict[str, Decimal] = {"short": Decimal("0.50"), "long": Decimal("3")}
MAX_OUTPUT_TOKENS: dict[str, int] = {"short": 1500, "long": 4000}
FILL_CHECK = "truth.fill"


def cost_cap_eur(length: PieceLength) -> Decimal:
    return COST_CAP_USD[length] * USD_TO_EUR


class Grader(Protocol):
    """Optional (WP-07 owns the graders). Higher is better; the note goes into the reason."""

    def grade(self, text: str, brief: Brief) -> tuple[float, str]: ...


class CopyStatus(StrEnum):
    READY_FOR_CHECKS = "ready_for_checks"  # a candidate built; variant in CHECKING
    NEEDS_HUMAN = "needs_human"  # nothing built after the repair loops; variant in DRAFTING
    NO_SOURCE = "no_source"
    STOPPED_COST_CAP = "stopped_cost_cap"
    PENDING_PERSON = "pending_person"  # a manual or unconfigured connection answered
    PROVIDER_ERROR = "provider_error"


@dataclass
class Attempt:
    number: int
    text: str
    compiled: CompileResult | None
    fill_problems: tuple[str, ...]
    connection_id: str
    provider: str
    model: str | None
    cost_eur: Decimal

    @property
    def failing_checks(self) -> list[str]:
        if self.compiled is None:
            return [FILL_CHECK]
        return sorted({f"truth.compile.{f.kind.value}" for f in self.compiled.blocks})


@dataclass
class CandidateRun:
    index: int
    attempts: list[Attempt] = field(default_factory=list)
    built: Built | None = None
    slop: list[SlopHit] = field(default_factory=list)
    status: Literal["open", "built", "failed", "stopped"] = "open"
    grade: tuple[float, str] | None = None

    @property
    def last(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None


@dataclass(frozen=True)
class CopyOutcome:
    status: CopyStatus
    note: str
    cap_eur: Decimal
    spent_eur: Decimal
    candidates: tuple[CandidateRun, ...] = ()
    asset: Asset | None = None
    bundle: Bundle | None = None
    variant: Variant | None = None
    variant_claims: tuple[VariantClaim, ...] = ()
    check_results: tuple[CheckResult, ...] = ()
    cost_entries: tuple[CostEntry, ...] = ()

    def rows(self) -> list[Any]:
        """In insert order: parents before children."""
        head = [r for r in (self.asset, self.bundle, self.variant) if r is not None]
        return [*head, *self.variant_claims, *self.check_results, *self.cost_entries]


class _Stop(Exception):
    def __init__(self, status: CopyStatus, note: str) -> None:
        super().__init__(note)
        self.status = status
        self.note = note


@dataclass
class _Run:
    registry: ConnectionRegistry
    tenant: Tenant
    brief: Brief
    clock: Clock
    cap: Decimal
    max_tokens: int
    scope: dict[str, str | None]
    spent: Decimal = Decimal(0)
    costs: list[CostEntry] = field(default_factory=list)

    def call(self, *, ref: str, instructions: str, data: Sequence[str]) -> tuple[str, Served]:
        remaining = self.cap - self.spent
        if remaining <= 0:
            raise _Stop(CopyStatus.STOPPED_COST_CAP, f"the {self.cap} EUR cap for this piece is used up")
        req = c.TextRequest(tenant_id=self.tenant.id, role=TextRole.DRAFT, instructions=instructions,
                            untrusted_data=tuple(data), language=self.brief.language,
                            max_output_tokens=self.max_tokens, idempotency_key=ref, cost_cap_eur=remaining)
        first = next(iter(self.registry.estimate_chain(self.tenant.id, req, **self.scope)), None)
        if first is not None and first[1] > remaining:
            raise _Stop(CopyStatus.STOPPED_COST_CAP,
                        f"stopped before a call estimated at up to {first[1]} EUR: {remaining} EUR is left of the "
                        f"{self.cap} EUR cap")
        try:
            served = self.registry.run(self.tenant.id, req, **self.scope)
        except c.CostCapExceeded:
            raise _Stop(CopyStatus.STOPPED_COST_CAP,
                        f"the registry refused a call over the {self.cap} EUR cap") from None
        except ConnectionsError as exc:
            raise _Stop(CopyStatus.PROVIDER_ERROR, f"{type(exc).__name__}: {exc}") from None
        result = served.result
        if served.mode is not ConnectionMode.MANUAL:
            out = result.output
            used = out.input_tokens + out.output_tokens if isinstance(out, c.TextResult) else 0
            self.costs.append(CostEntry(
                id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant.id,
                capability=Capability.TEXT, provider=served.provider, model=result.provenance.model,
                units=Decimal(used), cost_eur=result.cost_eur, run_ref=ref, subject_kind="brief",
                subject_id=self.brief.id, occurred_at=self.clock.now()))
            self.spent += result.cost_eur
        if result.status == "pending_person":
            raise _Stop(CopyStatus.PENDING_PERSON, result.task or "a person must write this piece")
        assert isinstance(result.output, c.TextResult)
        return result.output.text.strip(), served


def produce_copy(*, registry: ConnectionRegistry, tenant: Tenant, brief: Brief, claims: Mapping[str, Claim],
                 sources: Sequence[str], channel: Channel, rule_version: int, clock: Clock,
                 length: PieceLength = "short", max_chars: int | None = None, task_type: str = "post",
                 voice_rules: Sequence[str] = (), extra_slop: Sequence[str] = (), allowed_terms: Sequence[str] = (),
                 protected_names: Sequence[str] = (), grader: Grader | None = None, brand_id: str | None = None,
                 format: str | None = None, candidates: int = CANDIDATES) -> CopyOutcome:  # noqa: A002
    """`claims` is `truth.library(repo, tenant.id)`. `sources` are the source spans the piece may use
    (owner answers, staff material); they are treated as untrusted. `voice_rules` are approved
    brand rules; like every free-text brief field they travel as data, never as instructions.
    Nothing is written: pass the outcome to `write_outcome`."""
    if brief.tenant_id != tenant.id:
        raise ValueError("the brief belongs to another tenant")
    cap = cost_cap_eur(length)
    sources = [s for s in sources if s and s.strip()]
    if not sources:
        return CopyOutcome(CopyStatus.NO_SOURCE, "no source material: a brief with no source produces no draft",
                           cap, Decimal(0))
    run = _Run(registry, tenant, brief, clock, cap, MAX_OUTPUT_TOKENS[length],
               {"brand_id": brand_id, "format": format})
    shared = dict(brief=brief, channel=channel, language=brief.language, max_chars=max_chars, claims=claims)
    brief_item = prompts.brief_data(brief=brief, language=brief.language, voice_rules=voice_rules, claims=claims)
    runs: list[CandidateRun] = []
    stop: _Stop | None = None

    def check(text: str) -> tuple[Built | None, CompileResult | None, tuple[str, ...]]:
        if not text:
            return None, None, ("the writer returned no text",)
        try:
            built = build(text, tenant_id=tenant.id, language=brief.language, claims=claims, clock=clock,
                          default_language=tenant.default_language, allowed_claim_ids=brief.allowed_claim_ids,
                          protected_names=protected_names, allowed_terms=allowed_terms,
                          tenant_currency=tenant.currency)
        except CompileFailed as exc:
            return None, exc.result, ()
        except FillError as exc:
            return None, None, tuple(exc.problems)
        return built, built.compiled, ()

    for i in range(candidates):
        cand = CandidateRun(i)
        runs.append(cand)
        try:
            text, served = run.call(ref=f"copy:{brief.id}:c{i}:a1",
                                    instructions=prompts.draft_instructions(angle=i, **shared),
                                    data=[brief_item, *sources])
            for number in range(1, MAX_REPAIR_LOOPS + 2):
                built, compiled, problems = check(text)
                cand.attempts.append(Attempt(number, text, compiled, problems, served.connection_id, served.provider,
                                             served.result.provenance.model, served.result.cost_eur))
                if built is not None:
                    cand.built, cand.status = built, "built"
                    cand.slop = find_slop(text, brief.language, extra_slop)
                    if grader is not None:
                        cand.grade = grader.grade(built.filled.text, brief)
                    break
                if number > MAX_REPAIR_LOOPS:
                    cand.status = "failed"
                    break
                text, served = run.call(ref=f"copy:{brief.id}:c{i}:a{number + 1}",
                                        instructions=prompts.repair_instructions(**shared),
                                        data=[brief_item, prompts.findings_data(text, compiled, problems),
                                              *sources])
        except _Stop as s:
            if cand.status == "open":
                cand.status = "stopped"
            stop = s
            break

    return _finish(runs, stop, run, tenant=tenant, brief=brief, channel=channel, rule_version=rule_version,
                   task_type=task_type, max_chars=max_chars, clock=clock)


# ---------------------------------------------------------------------------- selection

def _over(cand: CandidateRun, max_chars: int | None) -> bool:
    return bool(max_chars) and cand.built is not None and len(cand.built.filled.text) > (max_chars or 0)


def _key(cand: CandidateRun, max_chars: int | None) -> tuple:
    assert cand.built is not None
    grade = -cand.grade[0] if cand.grade else 0.0
    return (grade, len(cand.built.flags), len(cand.slop), _over(cand, max_chars), len(cand.attempts), cand.index)


def _describe(cand: CandidateRun, total: int, max_chars: int | None) -> str:
    head = f"candidate {cand.index + 1} of {total}"
    last = cand.last
    if cand.built is not None:
        size = len(cand.built.filled.text)
        limit = f" of {max_chars}" if max_chars else ""
        if _over(cand, max_chars):
            limit = f" (over the {max_chars} limit)"
        bits = [f"compiled on attempt {len(cand.attempts)}", f"{len(cand.built.flags)} open flag(s)",
                f"{len(cand.slop)} slop hit(s)", f"{size} characters{limit}"]
        if cand.grade:
            bits.append(f"grader {cand.grade[0]:g}: {cand.grade[1]}")
        return f"{head}: " + ", ".join(bits)
    if cand.status == "failed" and last is not None:
        return f"{head}: failed after {len(cand.attempts)} attempts ({', '.join(last.failing_checks)})"
    return f"{head}: stopped after {len(cand.attempts)} attempt(s)"


def _summary(cand: CandidateRun) -> dict[str, Any]:
    last = cand.last
    return {
        "index": cand.index, "angle": prompts.ANGLES[cand.index % len(prompts.ANGLES)], "status": cand.status,
        "attempts": len(cand.attempts), "text": last.text if last else None,
        "rendered_text": cand.built.filled.text if cand.built else None,
        "connection_id": last.connection_id if last else None, "provider": last.provider if last else None,
        "model": last.model if last else None,
        "open_flags": [f"{f.kind.value}: {f.match}" for f in cand.built.flags] if cand.built else [],
        "failing_checks": last.failing_checks if last and not cand.built else [],
        "slop": [f"{h.kind.value}: {h.match}" for h in cand.slop],
        "grade": list(cand.grade) if cand.grade else None,
        "cost_eur": str(sum((a.cost_eur for a in cand.attempts), Decimal(0))),
    }


def _check_rows(cand: CandidateRun, variant_id: str, tenant_id: str, clock: Clock) -> list[CheckResult]:
    rows: list[CheckResult] = []
    seen: set[str] = set()
    for a in cand.attempts:
        if a.compiled is not None:
            new = to_check_results(a.compiled, variant_id=variant_id, clock=clock, attempt=a.number, model_id=a.model)
        else:
            new = [CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id, variant_id=variant_id,
                               layer=CheckLayer.CODE, check_name=FILL_CHECK, verdict=CheckVerdict.FAIL,
                               reason="; ".join(a.fill_problems), attempt=a.number, model_id=a.model)]
        names = {r.check_name for r in new}
        rows += [CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id, variant_id=variant_id,
                             layer=CheckLayer.CODE, check_name=name, verdict=CheckVerdict.PASS,
                             reason="no longer raised on this attempt", attempt=a.number, model_id=a.model)
                 for name in sorted(seen - names)]
        rows += new
        seen |= names
    return rows


def _finish(runs: list[CandidateRun], stop: _Stop | None, run: _Run, *, tenant: Tenant, brief: Brief,
            channel: Channel, rule_version: int, task_type: str, max_chars: int | None,
            clock: Clock) -> CopyOutcome:
    total = len(runs)
    built = [r for r in runs if r.built is not None]
    tried = [r for r in runs if r.attempts]
    base = dict(cap_eur=run.cap, spent_eur=run.spent, candidates=tuple(runs), cost_entries=tuple(run.costs))
    if not tried:
        assert stop is not None
        return CopyOutcome(stop.status, stop.note, **base)

    stopped = f" Stopped early: {stop.note}." if stop else ""
    if built:
        winner = min(built, key=lambda r: _key(r, max_chars))
        others = [_describe(r, total, max_chars) for r in runs if r is not winner]
        reason = (f"Selected {_describe(winner, total, max_chars)}."
                  + (f" Others: {'; '.join(others)}." if others else "") + stopped)
        status, state, note = CopyStatus.READY_FOR_CHECKS, VariantState.CHECKING, reason
    else:
        def blocks(r: CandidateRun) -> tuple[int, int]:
            last = r.last
            assert last is not None
            return (len(last.compiled.blocks) if last.compiled else 1, r.index)
        winner = min(tried, key=blocks)
        last = winner.last
        assert last is not None
        failing = ", ".join(last.failing_checks)
        reason = (f"No candidate compiled; sent to a person. Failing check: {failing} "
                  f"({_describe(winner, total, max_chars)}).{stopped}")
        status, state, note = CopyStatus.NEEDS_HUMAN, VariantState.DRAFTING, reason
    if state is not VariantState.DRAFTING:
        VARIANT.check(VariantState.DRAFTING, state)

    last = winner.last
    assert last is not None
    now = clock.now()
    asset = Asset(id=new_id(clock), created_at=now, tenant_id=tenant.id, brief_id=brief.id, master_text=last.text,
                  candidates=[_summary(r) for r in runs], selection_reason=reason, model_id=last.model,
                  prompt_version=prompts.PROMPT_VERSION)
    bundle = Bundle(id=new_id(clock), created_at=now, tenant_id=tenant.id, asset_id=asset.id, rule_version=rule_version)
    variant = Variant(id=new_id(clock), created_at=now, tenant_id=tenant.id, bundle_id=bundle.id, channel=channel,
                      language=brief.language, state=state, text=last.text,
                      rendered_text=winner.built.filled.text if winner.built else None, task_type=task_type,
                      model_id=last.model, prompt_version=prompts.PROMPT_VERSION)
    claim_ids = winner.built.compiled.claim_ids if winner.built else ()
    links = tuple(VariantClaim(id=new_id(clock), created_at=now, tenant_id=tenant.id, variant_id=variant.id,
                               claim_id=cid) for cid in dict.fromkeys(claim_ids))
    return CopyOutcome(status, note, asset=asset, bundle=bundle, variant=variant, variant_claims=links,
                       check_results=tuple(_check_rows(winner, variant.id, tenant.id, clock)), **base)
