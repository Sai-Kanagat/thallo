"""Shrinkage, so small numbers stay humble (BUILD_PLAN 2.6).

A rate here is outcomes per piece on one rung of the ladder. Each attribute's rate is a
Gamma-Poisson estimate pulled toward the brand's own average: the prior is worth PRIOR_PIECES
pieces at the brand rate, so the pull weakens as the attribute's own pieces grow.

PRIOR_PIECES = 20 is a starting default, not a benchmark. It is set so the spec's own example
stays humble on shrinkage alone: three bookings from four posts does not beat ten from forty.

The probability that one side beats another comes from a fixed-seed Monte Carlo over the two
posteriors, so the same rows always give the same number. Intervals are 90% credible intervals.
No pieces means no estimate (None), never zero.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

PRIOR_PIECES = 20
SMALL_SAMPLE = 8  # fewer pieces than this: labelled "small sample", and never enough to decide
DRAWS = 20_000
SEED = 18
INTERVAL = (0.05, 0.95)
FLOOR = 1e-6  # a brand rate of zero still gives a proper prior


@dataclass(frozen=True)
class Estimate:
    pieces: int
    events: int
    raw_rate: float | None
    rate: float | None  # shrunk toward the brand rate
    low: float | None
    high: float | None
    small_sample: bool

    def as_dict(self) -> dict:
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 4)

        return {"pieces": self.pieces, "events": self.events, "raw_rate": r(self.raw_rate), "rate": r(self.rate),
                "interval_90": [r(self.low), r(self.high)], "small_sample": self.small_sample,
                "label": "small sample" if self.small_sample else None}


def _shape_rate(events: int, pieces: int, brand_rate: float) -> tuple[float, float]:
    return max(brand_rate, FLOOR) * PRIOR_PIECES + events, PRIOR_PIECES + pieces


def _draws(events: int, pieces: int, brand_rate: float, rng: random.Random) -> list[float]:
    shape, rate = _shape_rate(events, pieces, brand_rate)
    return [rng.gammavariate(shape, 1 / rate) for _ in range(DRAWS)]


def _quantile(sorted_values: list[float], q: float) -> float:
    i = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[i]


def estimate(events: int, pieces: int, brand_rate: float) -> Estimate:
    if pieces <= 0:
        return Estimate(0, events, None, None, None, None, True)
    shape, rate = _shape_rate(events, pieces, brand_rate)
    rng = random.Random(SEED)  # noqa: S311 - a fixed seed for repeatable statistics, not security
    draws = sorted(_draws(events, pieces, brand_rate, rng))
    return Estimate(pieces, events, events / pieces, shape / rate, _quantile(draws, INTERVAL[0]),
                    _quantile(draws, INTERVAL[1]), pieces < SMALL_SAMPLE)


def prob_beats(a: tuple[int, int], b: tuple[int, int], brand_rate: float) -> float | None:
    """P(rate of a > rate of b), each given as (events, pieces). None when a side has no pieces."""
    if a[1] <= 0 or b[1] <= 0:
        return None
    rng = random.Random(SEED)  # noqa: S311 - a fixed seed for repeatable statistics, not security
    da, db = _draws(*a, brand_rate, rng), _draws(*b, brand_rate, rng)
    return round(sum(x > y for x, y in zip(da, db, strict=True)) / DRAWS, 4)


def prob_above_half(wins: int, n: int) -> float | None:
    """P(true beat rate > 0.5) under a flat Beta(1, 1) prior, by the same fixed-seed draws."""
    if n <= 0:
        return None
    rng = random.Random(SEED)  # noqa: S311 - a fixed seed for repeatable statistics, not security
    return round(sum(rng.betavariate(1 + wins, 1 + n - wins) > 0.5 for _ in range(DRAWS)) / DRAWS, 4)


__all__ = ["PRIOR_PIECES", "SMALL_SAMPLE", "Estimate", "estimate", "prob_above_half", "prob_beats"]
