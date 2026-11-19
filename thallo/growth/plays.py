"""The play library as typed, validated data.

A play is a row set, not code (BUILD_PLAN 2.1): channels, formats, qualification questions, what
counts as a booked conversation, a default pillar mix, fit rules, routes and the metrics that
matter. Plays A and B come from `docs/plays/plays_draft.json`; play C is designed for, not piloted.

Every number in a play is a starting default and a guess. None is a benchmark. They are replaced
by measured rates once 30 outcomes exist (see `thallo.growth.model.MEASURED_AFTER`).

Validation, all deterministic:
- pillar and stage shares are Decimal, sum to exactly 1, and each pillar is at least 0.10;
- stage mix keys are exactly the funnel stages;
- every fit rule names a question key (or a derived fact), uses a value that question can produce,
  and repeats the question's `only_if` condition;
- routes partition (fit, heat): every pair lands in exactly one route, and both the hot threshold
  and the low-fit band are reachable from some set of answers;
- fit stays inside 0..100 for every combination of answers.
"""
from __future__ import annotations

import itertools
import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from thallo.contracts.enums import Channel, FunnelStage, LeadRoute

DATA = Path(__file__).with_name("plays.json")
MIN_PILLAR_SHARE = Decimal("0.10")
EVIDENCE = "guess"  # every number in a play until 30 outcomes exist
GUESS_LABEL = "Starting default, a guess. Replaced by measured rates after 30 outcomes. Not a benchmark."

# Values a fit rule may test on a free-text or catalogue question. The answer itself is never
# matched; code that knows the tenant's lists (service area, stock, supported use cases,
# catalogue) turns the answer into one of these facts first. Anything else is "other".
PREDICATES = frozenset({"in_service_area", "in_stock", "matches_supported_list", "in_catalogue"})
# Facts about a lead that are not questions. email_domain comes from the lead's own address.
DERIVED_FACTS: dict[str, frozenset[str]] = {"email_domain": frozenset({"company", "personal"})}
OTHER = "other"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Question(_Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    ask: str = Field(min_length=3, max_length=200)
    type: Literal["text", "choice", "choice_from_catalogue"]
    options: tuple[str, ...] = ()
    only_if: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _options(self) -> Question:
        if self.type == "choice" and len(self.options) < 2:
            raise ValueError(f"question {self.key}: a choice needs at least two options")
        if self.type != "choice" and self.options:
            raise ValueError(f"question {self.key}: only a choice has options")
        if len(set(self.options)) != len(self.options):
            raise ValueError(f"question {self.key}: options repeat")
        return self

    def values(self) -> tuple[str, ...]:
        """Every value a fit rule could see for this question."""
        if self.type == "choice":
            return self.options
        return (*sorted(PREDICATES), OTHER)


class FitRule(_Strict):
    when: dict[str, str] = Field(alias="if", min_length=1)
    add: int = Field(ge=-50, le=50)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Band(_Strict):
    fit_min: int | None = Field(default=None, ge=0, le=100)
    fit_max: int | None = Field(default=None, ge=0, le=100)
    heat_min: int | None = Field(default=None, ge=0, le=100)


class Routes(_Strict):
    hot: Band
    warm: Band
    low_fit: Band

    @model_validator(mode="after")
    def _partition(self) -> Routes:
        h, w, lo = self.hot, self.warm, self.low_fit
        if h.fit_min is None or w.fit_min is None or lo.fit_max is None or h.heat_min is None:
            raise ValueError("routes need hot.fit_min, hot.heat_min, warm.fit_min and low_fit.fit_max")
        if h.fit_min != w.fit_min:
            raise ValueError("hot and warm must share one fit threshold")
        if lo.fit_max != w.fit_min - 1:
            raise ValueError("low_fit must end one point below the fit threshold")
        if (w.heat_min or 0) != 0 or h.heat_min <= 0:
            raise ValueError("warm starts at heat 0 and hot needs a heat above 0")
        if h.fit_max is not None or w.fit_max is not None or lo.fit_min is not None or lo.heat_min is not None:
            raise ValueError("routes take only the thresholds above")
        return self

    @property
    def fit_threshold(self) -> int:
        return self.warm.fit_min or 0

    def route(self, fit: int, heat: int) -> LeadRoute:
        if fit <= (self.low_fit.fit_max or 0):
            return LeadRoute.LOW_FIT
        return LeadRoute.HOT if heat >= (self.hot.heat_min or 0) else LeadRoute.WARM


class WeeklyVolume(_Strict):
    primary_pieces: int = Field(ge=1, le=21)
    max_channels_live: int = Field(ge=1, le=12)


class PlaySpec(_Strict):
    code: str = Field(pattern=r"^[A-Z](-[a-z0-9]{1,16})?$")
    name: str = Field(min_length=3, max_length=80)
    status: Literal["draft", "designed_not_piloted", "piloted"] = "draft"
    fits: str = ""
    channels: tuple[Channel, ...] = Field(min_length=1)
    formats: tuple[str, ...] = Field(min_length=1)
    booked_definition: str = Field(min_length=10, max_length=300)
    bottlenecks_in_order: tuple[str, ...] = Field(min_length=1)
    qualification_questions: tuple[Question, ...] = Field(min_length=1, max_length=6)
    fit_rules: tuple[FitRule, ...] = Field(min_length=1)
    fit_base: int = Field(ge=0, le=50)
    heat_actions: dict[str, int]
    routes: Routes
    pillar_mix: dict[str, Decimal]
    stage_mix: dict[FunnelStage, Decimal]
    metrics: tuple[str, ...] = Field(min_length=1)
    headline_metric: str
    weekly_volume: WeeklyVolume
    voice_questions_examples: tuple[str, ...] = ()
    shot_list_examples: tuple[str, ...] = ()
    never: tuple[str, ...] = Field(min_length=1)

    @field_validator("formats", "metrics")
    @classmethod
    def _slugs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for s in v:
            if not s or len(s) > 40 or not s.replace("_", "").isalnum() or s != s.lower():
                raise ValueError(f"{s!r} must be a lower-case slug")
        if len(set(v)) != len(v):
            raise ValueError("values repeat")
        return v

    @field_validator("channels")
    @classmethod
    def _channels_unique(cls, v: tuple[Channel, ...]) -> tuple[Channel, ...]:
        if len(set(v)) != len(v):
            raise ValueError("channels repeat")
        return v

    @field_validator("heat_actions")
    @classmethod
    def _heat(cls, v: dict[str, int]) -> dict[str, int]:
        if not v or any(not 1 <= n <= 100 for n in v.values()):
            raise ValueError("heat actions need points between 1 and 100")
        return v

    @model_validator(mode="after")
    def _check(self) -> PlaySpec:
        _shares("pillar_mix", self.pillar_mix, minimum=MIN_PILLAR_SHARE)
        if set(self.stage_mix) != set(FunnelStage):
            raise ValueError("stage_mix must name every funnel stage")
        _shares("stage_mix", {k.value: v for k, v in self.stage_mix.items()}, minimum=Decimal(0))
        if self.headline_metric not in self.metrics:
            raise ValueError("headline_metric must be one of the metrics")
        if self.weekly_volume.max_channels_live > len(self.channels):
            raise ValueError("max_channels_live exceeds the play's channels")
        qs = {q.key: q for q in self.qualification_questions}
        if len(qs) != len(self.qualification_questions):
            raise ValueError("question keys repeat")
        for q in self.qualification_questions:
            for k, val in q.only_if.items():
                if k not in qs or val not in qs[k].values():
                    raise ValueError(f"question {q.key}: only_if names {k}={val}, which no question produces")
        for r in self.fit_rules:
            for k, val in r.when.items():
                allowed = qs[k].values() if k in qs else DERIVED_FACTS.get(k)
                if allowed is None:
                    raise ValueError(f"fit rule tests {k}, which is neither a question nor a derived fact")
                if val not in allowed or val == OTHER:
                    raise ValueError(f"fit rule tests {k}={val}, which that question cannot produce")
                if k in qs:
                    for ck, cv in qs[k].only_if.items():
                        if r.when.get(ck) != cv:
                            raise ValueError(f"fit rule on {k} must also require {ck}={cv}")
        lo, hi = fit_range(self)
        if lo < 0 or hi > 100:
            raise ValueError(f"fit can reach {lo}..{hi}; keep it inside 0..100")
        if hi < self.routes.fit_threshold:
            raise ValueError("no set of answers reaches the fit threshold, so nothing is ever hot")
        if lo > (self.routes.low_fit.fit_max or 0):
            raise ValueError("no set of answers is low fit")
        return self

    # ---------------------------------------------------------------- scoring

    def fit(self, facts: dict[str, str]) -> int:
        """Fit from the rule table. `facts` holds choice answers as given and text or catalogue
        answers already turned into a predicate by the caller."""
        return self.fit_base + sum(r.add for r in self.fit_rules
                                   if all(facts.get(k) == v for k, v in r.when.items()))

    def route(self, facts: dict[str, str], heat: int) -> LeadRoute:
        return self.routes.route(self.fit(facts), heat)


def _shares(name: str, shares: dict[str, Decimal], *, minimum: Decimal) -> None:
    if not shares:
        raise ValueError(f"{name} is empty")
    for k, v in shares.items():
        if not k or not k.replace("_", "").isalnum() or k != k.lower():
            raise ValueError(f"{name}: {k!r} must be a lower-case slug")
        if v < minimum or v > 1:
            raise ValueError(f"{name}: {k} is {v}; each share must be between {minimum} and 1")
    total = sum(shares.values(), Decimal(0))
    if total != 1:
        raise ValueError(f"{name} sums to {total}; it must sum to exactly 1")


def _answer_space(spec: PlaySpec) -> list[dict[str, str]]:
    keys: list[str] = []
    values: list[tuple[str, ...]] = []
    for q in spec.qualification_questions:
        keys.append(q.key)
        values.append((*q.values(), ""))  # "" is unanswered
    tested = {k for r in spec.fit_rules for k in r.when}
    for k, vals in DERIVED_FACTS.items():
        if k in tested:
            keys.append(k)
            values.append((*sorted(vals), ""))
    out = []
    for combo in itertools.product(*values):
        facts = dict(zip(keys, combo, strict=True))
        # an only_if question is not asked unless its condition holds
        for q in spec.qualification_questions:
            if q.only_if and any(facts.get(k) != v for k, v in q.only_if.items()):
                facts[q.key] = ""
        out.append(facts)
    return out


def fit_range(spec: PlaySpec) -> tuple[int, int]:
    scores = [spec.fit(f) for f in _answer_space(spec)]
    return min(scores), max(scores)


# ---------------------------------------------------------------- the library

class Library(_Strict):
    plays: tuple[PlaySpec, ...]
    open_questions_for_sai: tuple[str, ...] = ()

    def get(self, code: str) -> PlaySpec | None:
        return next((p for p in self.plays if p.code == code), None)


def parse(raw: dict) -> Library:
    return Library.model_validate({k: v for k, v in raw.items() if not k.startswith("_")})


def load_text(text: str) -> Library:
    """Parse play JSON with every number as Decimal, so share sums are exact."""
    return parse(json.loads(text, parse_float=Decimal))


@lru_cache(maxsize=1)
def library() -> Library:
    lib = load_text(DATA.read_text(encoding="utf-8"))
    codes = [p.code for p in lib.plays]
    if len(set(codes)) != len(codes):
        raise ValueError("play codes repeat")
    return lib


def base_code(code: str) -> str:
    """A tuned copy of play A is coded `A-<slug>` and inherits A's rules."""
    return code.split("-", 1)[0]


def spec_view(spec: PlaySpec) -> dict:
    d = spec.model_dump(mode="json", by_alias=True)
    lo, hi = fit_range(spec)
    d["fit_range"] = [lo, hi]
    d["evidence"] = EVIDENCE
    d["evidence_label"] = GUESS_LABEL
    return d


__all__ = ["EVIDENCE", "GUESS_LABEL", "MIN_PILLAR_SHARE", "FitRule", "Library", "PlaySpec", "Question",
           "Routes", "base_code", "fit_range", "library", "load_text", "parse", "spec_view"]
