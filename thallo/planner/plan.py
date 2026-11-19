"""The weekly plan: slots with reasons, each tied to a source, a play, a pillar, a stage and the
one metric it is meant to move, with a sealed forecast where history allows.

Deterministic, no model call. The guards, in order (any one refuses the whole week):
- an active growth model with a play, the tenant's pillars and at least one verified claim
  (module 7: refuses to plan if claims or pillars are empty);
- no source, no slot: every slot takes one unused `source` that is not research (research is
  untrusted web text and may only suggest claim candidates). Fewer sources than the play's
  weekly volume means fewer slots, said in "not doing"; no source at all refuses the week;
- the week is planned once. A second run for the same week is refused and touches nothing.

Volume and mix:
- pieces = the play's `weekly_volume.primary_pieces`, on its organic channels (ads are the ads
  desk's), at most `max_channels_live` of them, round robin;
- stages by largest remainder on the play's `stage_mix`, so each stage is within one slot of its
  share; `stage_check` fails a week that is all awareness or misses a stage by a slot or more;
- experiments: round half up of 20% of the slots (EXPERIMENT_SHARE), so 1 of 3 to 7, 2 of 8
  to 12, none of 1 or 2. Experiment slots are marked `reserved_for_experiment` and try the
  play format used least in earlier weeks;
- pillars: the active model's pillars, the one furthest below its target share that matches the
  slot's stage, else any.

Brief text is fixed wording plus trusted names (play, pillar, stage). The source is linked by id
and never copied, because what people said is untrusted. Ideas stay PROPOSED: a human accepts.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from thallo.contracts.clock import Clock
from thallo.contracts.enums import Channel, FunnelStage, SourceKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Brief,
    Claim,
    Idea,
    IdeaSignal,
    Pillar,
    PlanSlot,
    Play,
    Signal,
    Source,
    Tenant,
)
from thallo.contracts.states import ClaimState
from thallo.growth.plays import PlaySpec
from thallo.growth.service import Refused as GrowthRefused
from thallo.growth.service import active_model, merged_spec
from thallo.planner import forecast as fc
from thallo.planner.records import PlannerSlot, PlannerWeek

EXPERIMENT_SHARE = Decimal("0.2")
ADS = frozenset({Channel.SEARCH_ADS, Channel.SOCIAL_ADS})
POST_HOUR = time(10, 0)
WEEKDAYS = 5
SIGNALS_PER_IDEA = 3
SIGNAL_DAYS = 14
PROMPT_VERSION = "planner.rules/1"

JOB = {FunnelStage.AWARENESS: "Notice the business and remember what it is for",
       FunnelStage.CONSIDERATION: "Compare options and see why this one fits",
       FunnelStage.CONVERSION: "Take the next step: ask, book or visit",
       FunnelStage.RETENTION: "Come back, buy again or tell someone"}
CTA = {FunnelStage.AWARENESS: "Follow for more like this",
       FunnelStage.CONSIDERATION: "See the details",
       FunnelStage.CONVERSION: "Message us to book",
       FunnelStage.RETENTION: "Tell us how it went"}


class PlanRefused(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def experiment_count(n: int) -> int:
    return int((Decimal(n) * EXPERIMENT_SHARE).quantize(Decimal(1), ROUND_HALF_UP))


def apportion(shares: dict[FunnelStage, Decimal], n: int) -> list[FunnelStage]:
    """Largest remainder: n slots across stages, in funnel order."""
    order = list(FunnelStage)
    exact = {s: shares.get(s, Decimal(0)) * n for s in order}
    got = {s: int(exact[s]) for s in order}
    left = n - sum(got.values())
    for s in sorted(order, key=lambda s: (-(exact[s] - got[s]), order.index(s)))[:left]:
        got[s] += 1
    return [s for s in order for _ in range(got[s])]


def stage_check(stages: list[FunnelStage], shares: dict[FunnelStage, Decimal]) -> dict:
    n = len(stages)
    counts = Counter(stages)
    rows, problems = [], []
    for s in FunnelStage:
        target = float(shares.get(s, Decimal(0)))
        rows.append({"stage": s.value, "slots": counts[s], "share": round(counts[s] / n, 4) if n else 0.0,
                     "target": round(target, 4)})
        if n and abs(counts[s] - target * n) >= 1:
            problems.append(f"{s.value}: {counts[s]} slot(s) against a target of {target * n:.1f}")
    if n and counts[FunnelStage.AWARENESS] == n:
        problems.append("every slot is awareness: top of funnel only")
    return {"ok": not problems, "stages": rows, "problems": problems}


@dataclass(frozen=True)
class Planned:
    week: PlannerWeek
    slots: list[PlanSlot]


def _inputs(tx: Any, tenant: Tenant) -> tuple[Any, PlaySpec, list[Pillar], list[Claim], list[str]]:
    problems: list[str] = []
    model = active_model(tx)
    spec = None
    if model is None or model.play_id is None:
        problems.append("There is no growth model with a play yet, so there is nothing to plan against.")
    else:
        play = tx.get(Play, model.play_id)
        if play is None:
            problems.append("The growth model's play is missing.")
        else:
            try:
                spec = merged_spec(play, play.code)
            except GrowthRefused as e:
                problems += e.problems
    pillars = [p for p in tx.list_all(Pillar) if model is not None and p.growth_model_id == model.id]
    if not pillars:
        problems.append("There are no pillars for the active growth model.")
    claims = [c for c in tx.list_all(Claim) if c.state is ClaimState.VERIFIED]
    if not claims:
        problems.append("There is no verified claim yet, so no piece could say anything checked.")
    return model, spec, pillars, claims, problems


def _used_sources(tx: Any) -> set[str]:
    planned = {s.idea_id for s in tx.list_all(PlannerSlot)}
    return {sid for i in tx.list_all(Idea) if i.id in planned for sid in i.source_ids}


def plan_week(tx: Any, clock: Clock, tenant: Tenant, week_start: date) -> Planned:  # noqa: C901, PLR0914, PLR0915
    week_start = monday(week_start)
    if tx.list_all(PlannerWeek, week_start=week_start):
        raise PlanRefused([f"The week of {week_start.isoformat()} is already planned."])
    model, spec, pillars, claims, problems = _inputs(tx, tenant)
    used = _used_sources(tx)
    sources = sorted((s for s in tx.list_all(Source) if s.kind is not SourceKind.RESEARCH and s.id not in used),
                     key=lambda s: (s.captured_at, s.id), reverse=True)
    if not sources:
        problems.append("No unused source (a voice answer, a note, a document). No source, no slot.")
    if problems or spec is None:
        raise PlanRefused(problems)

    now = clock.now()
    not_doing: list[str] = []
    organic = [c for c in spec.channels if c not in ADS]
    channels = organic[: spec.weekly_volume.max_channels_live]
    if any(c in ADS for c in spec.channels):
        not_doing.append("No ad slots: paid placements go through the ads desk with a signed proposal.")
    if len(organic) > len(channels):
        skipped = ", ".join(c.value for c in organic[len(channels):])
        not_doing.append(f"Not posting on {skipped}: the play keeps {len(channels)} channels live at once.")
    if not channels:
        raise PlanRefused(["The play has no organic channel to plan for."])
    wanted = spec.weekly_volume.primary_pieces
    n = min(wanted, len(sources))
    if n < wanted:
        not_doing.append(f"{wanted - n} of {wanted} slots left empty: only {n} unused source(s). No source, no slot.")
    if len(sources) > n:
        not_doing.append(f"{len(sources) - n} source(s) kept for later weeks.")

    stages = apportion(spec.stage_mix, n)
    check = stage_check(stages, spec.stage_mix)
    n_exp = experiment_count(n)
    by_claim_source: dict[str, list[str]] = {}
    for c in claims:
        if c.source_id:
            by_claim_source.setdefault(c.source_id, []).append(c.id)
    earlier_formats = Counter(s.format for s in tx.list_all(PlannerSlot))
    signals = sorted((s for s in tx.list_all(Signal) if s.observed_at >= now - timedelta(days=SIGNAL_DAYS)),
                     key=lambda s: (s.observed_at, s.id), reverse=True)
    pieces = fc.history(tx, now)
    zone = ZoneInfo(tenant.timezone)
    total_share = sum((Decimal(p.target_share) for p in pillars), Decimal(0)) or Decimal(1)
    assigned: Counter[str] = Counter()
    week = tx.insert(PlannerWeek(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, week_start=week_start,
                                 play_code=spec.code, growth_model_version=model.version,
                                 reasoning=["-", "-"], not_doing=[], stage_check=check,
                                 planned_slots=n, experiment_slots=n_exp))
    slots: list[PlanSlot] = []
    forecasts = 0
    for i, stage in enumerate(stages):
        experiment = i >= n - n_exp
        channel = channels[i % len(channels)]
        source = sources[i]

        def deficit(p: Pillar) -> tuple:
            return (-(Decimal(p.target_share) / total_share * n - assigned[p.id]), p.name, p.id)
        matching = [p for p in pillars if p.stage is stage]
        pillar = min(matching or pillars, key=deficit)
        assigned[pillar.id] += 1
        if experiment:
            fmt = min(spec.formats, key=lambda f: (earlier_formats[f], spec.formats.index(f)))
        else:
            fmt = spec.formats[i % len(spec.formats)]
        earlier_formats[fmt] += 1
        metric = fc.STAGE_METRIC[stage]
        day = week_start + timedelta(days=round(i * WEEKDAYS / n) if n else 0)
        starts = datetime.combine(day, POST_HOUR, zone)
        kind = "Experiment" if experiment else "Piece"
        reason = (f"{kind} for play {spec.code}, pillar {pillar.name} at {stage.value}, to move {metric} by day "
                  f"{int(fc.DAY)}; from the {source.kind.value} source of {source.captured_at.date().isoformat()}.")
        idea = tx.insert(Idea(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, pillar_id=pillar.id,
                              stage=stage, title=f"{pillar.name}: {fmt.replace('_', ' ')} on {channel.value}",
                              rationale=reason, source_ids=[source.id], is_experiment=experiment,
                              prompt_version=PROMPT_VERSION))
        for sig in [s for s in signals if s.channel in (channel, None)][:SIGNALS_PER_IDEA]:
            tx.insert(IdeaSignal(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, idea_id=idea.id,
                                 signal_id=sig.id))
        brief = tx.insert(Brief(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, idea_id=idea.id,
                                audience=spec.fits, job_to_be_done=JOB[stage],
                                message=f"{pillar.name}, told from the owner's own source (linked, not copied).",
                                channels=[channel], allowed_claim_ids=by_claim_source.get(source.id, []),
                                call_to_action=CTA[stage], language=tenant.default_language,
                                prompt_version=PROMPT_VERSION))
        prop = fc.propose(pieces, metric, channel, stage, experiment=experiment)
        f_id, why_not = None, None
        if isinstance(prop, str):
            why_not = prop
        else:
            f_id = fc.seal(tx, forecast_id=new_id(clock), brief_id=brief.id, proposal=prop, now=now).id
            forecasts += 1
            reason = f"{reason} Forecast from {prop.basis}."[:500]
        slot = tx.insert(PlanSlot(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, week_start=week_start,
                                  channel=channel, pillar_id=pillar.id, stage=stage, starts_at=starts,
                                  reserved_for_experiment=experiment))
        tx.insert(PlannerSlot(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, week_id=week.id,
                              slot_id=slot.id, idea_id=idea.id, brief_id=brief.id, forecast_id=f_id,
                              no_forecast_reason=why_not, play_code=spec.code, target_metric=metric,
                              metric_day=int(fc.DAY), format=fmt, reason=reason))
        slots.append(slot)

    unused = sorted(p.name for p in pillars if not assigned[p.id])
    if unused:
        not_doing.append(f"No slot for pillar {', '.join(unused)} this week: fewer slots than pillars.")
    if forecasts < n:
        not_doing.append(f"{n - forecasts} slot(s) without a forecast: under {fc.MIN_HISTORY} measured pieces.")
    mix = ", ".join(f"{r['slots']} {r['stage']}" for r in check["stages"] if r["slots"])
    reasoning = [
        f"Play {spec.code} ({spec.name}): {n} piece(s) on {', '.join(c.value for c in channels)}, {mix}, "
        f"following the play's stage mix; first bottleneck to test: {spec.bottlenecks_in_order[0]}.",
        f"{n_exp} experiment slot(s) (20% kept for new ideas); every slot comes from its own source and "
        f"names its metric; {forecasts} forecast(s) sealed against a naive baseline.",
    ]
    tx.update(PlannerWeek, week.id, reasoning=reasoning, not_doing=not_doing)
    return Planned(tx.get(PlannerWeek, week.id), slots)


__all__ = ["EXPERIMENT_SHARE", "PlanRefused", "Planned", "apportion", "experiment_count", "monday", "plan_week",
           "stage_check"]
