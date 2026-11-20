"""Interval forecasts, sealed with a naive baseline and scored with the interval score.

The metric. A forecast is for one number per piece that the system can count by itself today:
tracked touches (WP-12) on any variant of the piece's brief, from the first send to day 14.

    link_clicks   touches of kind link
    enquiries     touches of kind whatsapp_code, form, call or walk_in_code

Self-reported touches ("how did you hear about us") are left out: they are not tied to a day.

History. One value per past brief that went live at least `day` days ago. A forecast needs at
least MIN_HISTORY values of its metric; with fewer, nothing is sealed and the slot says why.
That is the cold start rule: no history, no honest baseline, so no number is invented.

    forecast  10th to 90th percentile of the closest history: same channel and stage if there
              are MIN_HISTORY such pieces, else same channel, else all pieces. An experiment's
              interval is widened by EXPERIMENT_WIDEN around its middle: it is a new idea.
    baseline  the naive forecast: 10th to 90th percentile of the last BASELINE_WINDOW pieces,
              whatever their channel or stage.

Both are labelled guesses until scored. When the forecast uses all pieces it equals the
baseline, and the view says so: there was nothing better to go on.

Score. The interval score for a central (1 - ALPHA) interval [l, u] and the actual y
(Gneiting and Raftery 2007): (u - l) + (2 / ALPHA)(l - y) if y < l, + (2 / ALPHA)(y - u) if
y > u. Lower is better. The forecast beats the baseline when its score is strictly lower. A
missing actual scores None, never zero.

Seal. `seal_hash` is the sha256 of the stored columns plus `prev_hash`, chained per tenant by
`seq`, so a changed number or a removed forecast breaks `verify_chain`.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from thallo.contracts import sha256
from thallo.contracts.enums import Channel, FunnelStage, MetricDay, TouchKind
from thallo.contracts.models import (
    Asset,
    Brief,
    Bundle,
    Forecast,
    ForecastScore,
    Idea,
    Publication,
    Touch,
    Variant,
)

ALPHA = 0.2  # an 80% central interval: the 10th to the 90th percentile
LOW_Q, HIGH_Q = ALPHA / 2, 1 - ALPHA / 2
MIN_HISTORY = 3
BASELINE_WINDOW = 10
EXPERIMENT_WIDEN = 1.5
DAY = MetricDay.D14
GUESS_LABEL = "A guess until it is scored against what happened."

METRIC_KINDS: dict[str, frozenset[TouchKind]] = {
    "link_clicks": frozenset({TouchKind.LINK}),
    "enquiries": frozenset({TouchKind.WHATSAPP_CODE, TouchKind.FORM, TouchKind.CALL, TouchKind.WALK_IN_CODE}),
}
# The one number each stage is meant to move. Planner defaults, not benchmarks.
STAGE_METRIC: dict[FunnelStage, str] = {
    FunnelStage.AWARENESS: "link_clicks",
    FunnelStage.CONSIDERATION: "link_clicks",
    FunnelStage.CONVERSION: "enquiries",
    FunnelStage.RETENTION: "enquiries",
}


# ---------------------------------------------------------------- the numbers

def quantile(values: list[float], q: float) -> float:
    """Linear interpolation between order statistics (numpy's default)."""
    if not values:
        raise ValueError("no values")
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def interval(values: list[float]) -> tuple[float, float]:
    return round(quantile(values, LOW_Q), 4), round(quantile(values, HIGH_Q), 4)


def widen(low: float, high: float, factor: float) -> tuple[float, float]:
    mid, half = (low + high) / 2, (high - low) / 2 * factor
    return round(max(0.0, mid - half), 4), round(mid + half, 4)


def interval_score(low: float, high: float, actual: float | None, alpha: float = ALPHA) -> float | None:
    if actual is None:
        return None
    if low > high:
        raise ValueError("low must not be above high")
    s = high - low
    if actual < low:
        s += 2 / alpha * (low - actual)
    elif actual > high:
        s += 2 / alpha * (actual - high)
    return round(s, 6)


# ---------------------------------------------------------------- history from the tenant's rows

@dataclass(frozen=True)
class Piece:
    brief_id: str
    channel: Channel | None
    stage: FunnelStage | None
    sent_at: datetime
    values: dict[str, float]  # metric -> touches by day 14


def _publications_by_brief(tx: Any) -> dict[str, tuple[datetime, list[str]]]:
    """brief id -> (first send time, variant ids that went live)."""
    assets = {a.id: a.brief_id for a in tx.list_all(Asset)}
    bundles = {b.id: assets.get(b.asset_id) for b in tx.list_all(Bundle)}
    variants = {v.id: bundles.get(v.bundle_id) for v in tx.list_all(Variant)}
    out: dict[str, tuple[datetime, list[str]]] = {}
    for p in tx.list_all(Publication):
        brief_id = variants.get(p.variant_id)
        if brief_id is None or p.sent_at is None:
            continue
        first, ids = out.get(brief_id, (p.sent_at, []))
        out[brief_id] = (min(first, p.sent_at), [*ids, p.variant_id])
    return out


def count(touches: Iterable[Touch], variant_ids: Iterable[str], start: datetime, metric: str,
          day: int = int(DAY)) -> float:
    ids, kinds, end = set(variant_ids), METRIC_KINDS[metric], start + timedelta(days=day)
    return float(sum(1 for t in touches if t.variant_id in ids and t.kind in kinds and start <= t.occurred_at < end))


def history(tx: Any, now: datetime, day: int = int(DAY)) -> list[Piece]:
    """Every brief that went live at least `day` days before `now`, oldest first."""
    pubs = _publications_by_brief(tx)
    if not pubs:
        return []
    briefs = {b.id: b for b in tx.list_all(Brief)}
    ideas = {i.id: i for i in tx.list_all(Idea)}
    touches = tx.list_all(Touch)
    out = []
    for brief_id, (sent, vids) in pubs.items():
        if sent + timedelta(days=day) > now or brief_id not in briefs:
            continue
        b = briefs[brief_id]
        idea = ideas.get(b.idea_id)
        out.append(Piece(brief_id, b.channels[0] if b.channels else None, idea.stage if idea else None, sent,
                         {m: count(touches, vids, sent, m, day) for m in METRIC_KINDS}))
    return sorted(out, key=lambda p: (p.sent_at, p.brief_id))


@dataclass(frozen=True)
class Proposal:
    metric: str
    low: float
    high: float
    baseline_low: float
    baseline_high: float
    basis: str  # which history the forecast used, in words


def propose(pieces: list[Piece], metric: str, channel: Channel, stage: FunnelStage, *,
            experiment: bool = False) -> Proposal | str:
    """An interval and a baseline, or the reason there is none."""
    if metric not in METRIC_KINDS:
        raise ValueError(f"unknown metric {metric}")
    all_values = [p.values[metric] for p in pieces]
    if len(all_values) < MIN_HISTORY:
        return (f"No forecast: {len(all_values)} measured piece(s) so far, {MIN_HISTORY} are needed "
                "before a range can be said.")
    b_low, b_high = interval(all_values[-BASELINE_WINDOW:])
    same_both = [p.values[metric] for p in pieces if p.channel is channel and p.stage is stage]
    same_channel = [p.values[metric] for p in pieces if p.channel is channel]
    if len(same_both) >= MIN_HISTORY:
        values, basis = same_both, f"{len(same_both)} past pieces on this channel at this stage"
    elif len(same_channel) >= MIN_HISTORY:
        values, basis = same_channel, f"{len(same_channel)} past pieces on this channel"
    else:
        values, basis = all_values, f"all {len(all_values)} past pieces, the same as the baseline"
    low, high = interval(values)
    if experiment:
        low, high = widen(low, high, EXPERIMENT_WIDEN)
        basis += ", widened because it is an experiment"
    return Proposal(metric, low, high, b_low, b_high, basis)


# ---------------------------------------------------------------- seal and verify

def _utc(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def seal_digest(f: Forecast) -> str:
    return sha256({"seq": f.seq, "tenant_id": f.tenant_id, "brief_id": f.brief_id, "metric": f.metric,
                   "day": int(f.day), "low": f.low, "high": f.high, "baseline_low": f.baseline_low,
                   "baseline_high": f.baseline_high, "sealed_at": _utc(f.sealed_at), "prev_hash": f.prev_hash})


PROMPT_VERSION = "planner.forecast/1"  # rule-based: no model call, the version names the rule


def seal(tx: Any, *, forecast_id: str, brief_id: str, proposal: Proposal, now: datetime) -> Forecast:
    prior = max(tx.list_all(Forecast), key=lambda f: f.seq, default=None)
    draft = Forecast(id=forecast_id, created_at=now, tenant_id=tx.tenant_id, brief_id=brief_id,
                     seq=(prior.seq + 1) if prior else 1, metric=proposal.metric, day=DAY,
                     low=proposal.low, high=proposal.high, baseline_low=proposal.baseline_low,
                     baseline_high=proposal.baseline_high, sealed_at=now.astimezone(UTC),
                     prev_hash=prior.seal_hash if prior else None, seal_hash="0" * 64,
                     prompt_version=PROMPT_VERSION)
    return tx.insert(draft.model_copy(update={"seal_hash": seal_digest(draft)}))


def verify_chain(tx: Any, anchor: tuple[int, str] | None = None) -> tuple[bool, int | None]:
    """(True, None) when every forecast matches its seal and links to the one before, and the
    newest one is the anchor; else (False, the first seq that fails).

    The anchor is (seq, seal_hash) of the head. Without one passed, the `forecast_head` row the
    database trigger keeps is used (Postgres); with neither, a dropped tail cannot be seen."""
    from thallo.planner.records import ForecastHead  # noqa: PLC0415

    if anchor is None:
        heads = tx.list_all(ForecastHead)
        anchor = (heads[0].seq, heads[0].seal_hash) if heads else None
    chain = sorted(tx.list_all(Forecast), key=lambda f: f.seq)
    prev: str | None = None
    for n, f in enumerate(chain, start=1):
        if f.seq != n or f.prev_hash != prev or seal_digest(f) != f.seal_hash:
            return False, f.seq
        prev = f.seal_hash
    if anchor is not None:
        last = chain[-1] if chain else None
        if last is None or last.seq != anchor[0]:
            return False, (last.seq + 1) if last else 1
        if last.seal_hash != anchor[1]:
            return False, last.seq
    return True, None


# ---------------------------------------------------------------- scoring

def score(tx: Any, forecast: Forecast, actual: float | None, now: datetime, *, score_id: str) -> ForecastScore:
    """Score once. A second call returns the score already on record."""
    done = tx.list_all(ForecastScore, forecast_id=forecast.id)
    if done:
        return done[0]
    fs = interval_score(forecast.low, forecast.high, actual)
    bs = interval_score(forecast.baseline_low, forecast.baseline_high, actual)
    return tx.insert(ForecastScore(id=score_id, created_at=now, tenant_id=tx.tenant_id, forecast_id=forecast.id,
                                   actual=actual, interval_score=fs, baseline_score=bs,
                                   beat_baseline=None if fs is None or bs is None else fs < bs, scored_at=now))


def score_due(tx: Any, now: datetime, new_id: Any) -> list[ForecastScore]:
    """Score every unscored forecast whose piece went live at least its day ago, from the touch
    log. A piece that never went live is left unscored. WP-18 may call `score` with other actuals."""
    scored = {s.forecast_id for s in tx.list_all(ForecastScore)}
    pubs = _publications_by_brief(tx)
    touches = None
    out = []
    for f in sorted(tx.list_all(Forecast), key=lambda f: f.seq):
        if f.id in scored or f.brief_id not in pubs:
            continue
        sent, vids = pubs[f.brief_id]
        if sent + timedelta(days=int(f.day)) > now or f.metric not in METRIC_KINDS:
            continue
        touches = touches if touches is not None else tx.list_all(Touch)
        out.append(score(tx, f, count(touches, vids, sent, f.metric, int(f.day)), now, score_id=new_id()))
    return out


def view(f: Forecast | None, s: ForecastScore | None) -> dict | None:
    if f is None:
        return None
    out = {"id": f.id, "seq": f.seq, "metric": f.metric, "day": int(f.day), "low": f.low, "high": f.high,
           "baseline_low": f.baseline_low, "baseline_high": f.baseline_high,
           "sealed_at": f.sealed_at.isoformat(), "seal_hash": f.seal_hash, "scored": s is not None,
           "label": GUESS_LABEL if s is None else "Scored."}
    if s is not None:
        out.update(actual=s.actual, interval_score=s.interval_score, baseline_score=s.baseline_score,
                   beat_baseline=s.beat_baseline)
    return out


__all__ = ["ALPHA", "BASELINE_WINDOW", "DAY", "EXPERIMENT_WIDEN", "GUESS_LABEL", "METRIC_KINDS", "MIN_HISTORY",
           "STAGE_METRIC", "Piece", "Proposal", "count", "history", "interval", "interval_score", "propose",
           "quantile", "score", "score_due", "seal", "seal_digest", "verify_chain", "view", "widen"]
