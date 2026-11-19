"""Reads and writes for the growth model and the play editor, over one tenant transaction.

`tx` is any tenant-scoped repository with `get`, `list_all`, `insert` and `update` (the store's
TenantRepo or the API's memory tables). Nothing here calls a model, the network or a provider.

Play rows hold what the editor changes (name, channels, formats, questions, booked definition,
pillar mix, metrics). Fit rules, routes, heat points and stage mix come from the library play
the row is coded after (`A`, or `A-<slug>` for a tuned copy). Every save re-validates the merged
play and checks it against the tenant's current region policy; a contradiction or a missing
policy refuses the save.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from thallo.contracts.clock import Clock
from thallo.contracts.ids import new_id
from thallo.contracts.models import GrowthModel, Play, RegionPolicy, Tenant
from thallo.growth import model as gm
from thallo.growth.plays import PlaySpec, base_code, library, spec_view
from thallo.growth.policy import PolicyResult, PolicyStatus, check_play, current_policy

EDITABLE = ("name", "channels", "formats", "qualification_questions", "booked_definition", "pillar_mix", "metrics")


class Refused(ValueError):
    """A save the guard will not make. `problems` are plain sentences for the operator."""

    def __init__(self, problems: list[str], policy: PolicyResult | None = None) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems
        self.policy = policy


def _errors(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        where = ".".join(str(p) for p in err["loc"])
        msg = str(err["msg"]).removeprefix("Value error, ")
        out.append(f"{where}: {msg}" if where else msg)
    return out


def merged_spec(row: Play | None, code: str, edits: dict[str, Any] | None = None) -> PlaySpec:
    base = library().get(base_code(code))
    if base is None:
        raise Refused([f"There is no library play {base_code(code)} to build {code} on."])
    d = base.model_dump(mode="python", by_alias=True)
    d["code"] = code
    if row is not None:
        for k in EDITABLE:
            v = getattr(row, k)
            d[k] = {p: Decimal(str(s)) for p, s in v.items()} if k == "pillar_mix" else v
    for k, v in (edits or {}).items():
        if k not in EDITABLE:
            raise Refused([f"{k} cannot be edited here."])
        d[k] = v
    if code != base.code and row is None and not edits:
        d["status"] = "draft"
    try:
        return PlaySpec.model_validate(d)
    except ValidationError as e:
        raise Refused(_errors(e)) from None


def _policy(tx: Any, clock: Clock, tenant: Tenant) -> RegionPolicy | None:
    return current_policy(tx.list_all(RegionPolicy), tenant.region_code, clock.now())


def plays_view(tx: Any, clock: Clock, tenant: Tenant) -> list[dict]:
    rows = {r.code: r for r in tx.list_all(Play)}
    policy = _policy(tx, clock, tenant)
    codes = [p.code for p in library().plays] + sorted(c for c in rows if library().get(c) is None)
    out = []
    for code in codes:
        try:
            spec = merged_spec(rows.get(code), code)
        except Refused as e:  # a stored row that no longer validates is shown, not hidden
            out.append({"code": code, "invalid": e.problems, "saved": True})
            continue
        v = spec_view(spec)
        v["saved"] = code in rows
        v["play_id"] = rows[code].id if code in rows else None
        v["policy"] = check_play(spec, policy, tenant.region_code).view()
        out.append(v)
    return out


def save_play(tx: Any, clock: Clock, tenant: Tenant, code: str, edits: dict[str, Any]) -> tuple[Play, PolicyResult]:
    rows = {r.code: r for r in tx.list_all(Play, code=code)}
    row = rows.get(code)
    spec = merged_spec(row, code, edits)
    result = check_play(spec, _policy(tx, clock, tenant), tenant.region_code)
    if result.status is not PolicyStatus.OK:
        raise Refused(result.problems, result)
    fields = {
        "name": spec.name, "channels": list(spec.channels), "formats": list(spec.formats),
        "qualification_questions": [q.model_dump(mode="json") for q in spec.qualification_questions],
        "booked_definition": spec.booked_definition,
        "pillar_mix": {k: float(v) for k, v in spec.pillar_mix.items()},
        "metrics": list(spec.metrics),
    }
    if row is None:
        row = tx.insert(Play(id=new_id(clock), tenant_id=tenant.id, created_at=clock.now(), code=code, **fields))
    else:
        tx.update(Play, row.id, **fields)
        row = tx.get(Play, row.id)
    return row, result


def active_model(tx: Any) -> GrowthModel | None:
    rows = tx.list_all(GrowthModel)
    live = [r for r in rows if r.active]
    return max(live or rows, key=lambda r: r.version, default=None)


def model_view(row: GrowthModel | None, tenant: Tenant, plays: dict[str, Play]) -> dict | None:
    if row is None or row.margin_per_won is None:
        return None
    code = next((c for c, p in plays.items() if p.id == row.play_id), None)
    return {
        "version": row.version, "play_code": code, "stages": row.stages, "bottleneck": row.bottleneck,
        "margin_per_won": str(row.margin_per_won), "share_of_margin": str(row.share_of_margin),
        "summary": gm.summary(row.stages, row.margin_per_won, row.share_of_margin, tenant.currency),
    }


def save_model(tx: Any, clock: Clock, tenant: Tenant, answers: gm.OwnerAnswers) -> GrowthModel:
    """A new version from the owner's answers. Every rate is stored GUESSED."""
    plays = {r.code: r for r in tx.list_all(Play)}
    play = plays.get(answers.play_code)
    if play is None:
        play, _ = save_play(tx, clock, tenant, answers.play_code, {})
    spec = merged_spec(play, answers.play_code)
    stages = gm.guessed_stages(answers)
    s = gm.summary(stages, answers.margin_per_won, answers.share_of_margin, tenant.currency)
    prior = tx.list_all(GrowthModel)
    for r in prior:
        if r.active:
            tx.update(GrowthModel, r.id, active=False)
    version = max((r.version for r in prior), default=0) + 1
    return tx.insert(GrowthModel(
        id=new_id(clock), tenant_id=tenant.id, created_at=clock.now(), version=version, play_id=play.id,
        stages=stages, bottleneck=gm.bottleneck(spec.bottlenecks_in_order),
        margin_per_won=answers.margin_per_won, share_of_margin=answers.share_of_margin,
        # The column is named _eur but holds the tenant's currency (Wave 2 rule 3); see the report.
        allowable_cost_eur=Decimal(s["allowable_cost"]), active=True))


__all__ = ["EDITABLE", "Refused", "active_model", "merged_spec", "model_view", "plays_view", "save_model", "save_play"]
