"""The growth model: how this business gets customers, with numbers, and which bottleneck to test.

Module 2's guard: every rate is tagged guessed or measured. Whatever the owner types is GUESSED;
the server sets the tag, never the form. A rate becomes MEASURED only through `measure()`, given
the outcome counts behind it, and only once there are at least 30 (MEASURED_AFTER).

Allowable cost per booked conversation (BUILD_PLAN 2.6), in the tenant's currency:

    A = margin per won deal x show rate x close rate x share of margin the owner will spend

The share defaults to 0.30 and the owner sets it. The per-channel multiples of A and the
stopping rule (3 x A spent with no booking) are the plan's starting defaults, not benchmarks.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from thallo.contracts.enums import RateTag

MEASURED_AFTER = 30
DEFAULT_SHARE = Decimal("0.30")
CENT = Decimal("0.01")
STOP_MULTIPLE = Decimal(3)

StageName = Literal["enquiry_to_booked", "booked_to_held", "held_to_won"]
STAGES: tuple[tuple[StageName, str], ...] = (
    ("enquiry_to_booked", "Enquiries that become a booked conversation"),
    ("booked_to_held", "Booked conversations that are held (show rate)"),
    ("held_to_won", "Held conversations that are won (close rate)"),
)
REQUIRED: frozenset[str] = frozenset({"booked_to_held", "held_to_won"})

# BUILD_PLAN 2.6, "my starting defaults, not benchmarks [E]". Retuned after 90 days of own data.
CHANNEL_TARGETS: tuple[tuple[str, Decimal, str], ...] = (
    ("search_high_intent", Decimal("0.6"), "Search on brand and high-intent terms"),
    ("local_social", Decimal("1.0"), "Local social, click-to-WhatsApp or lead forms"),
    ("boost_organic", Decimal("1.0"), "Boosting an organic piece that beat its forecast"),
    ("retargeting", Decimal("0.8"), "Retargeting, capped at 20% of budget"),
    ("linkedin_named_accounts", Decimal("1.5"), "LinkedIn to a named account list, first 90 days"),
)


class Rate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rate: Decimal = Field(gt=0, le=1)
    low: Decimal | None = Field(default=None, gt=0, le=1)
    high: Decimal | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def _range(self) -> Rate:
        lo = self.low if self.low is not None else self.rate
        hi = self.high if self.high is not None else self.rate
        if not lo <= self.rate <= hi:
            raise ValueError("a range must hold its rate: low <= rate <= high")
        return self


class OwnerAnswers(BaseModel):
    """The growth model form. Every rate here is the owner's guess."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    play_code: str = Field(pattern=r"^[A-Z](-[a-z0-9]{1,16})?$")
    margin_per_won: Decimal = Field(gt=0, le=Decimal("10000000"), decimal_places=2)
    share_of_margin: Decimal = Field(default=DEFAULT_SHARE, gt=0, le=1)
    rates: dict[StageName, Rate]

    @model_validator(mode="after")
    def _needs(self) -> OwnerAnswers:
        missing = REQUIRED - set(self.rates)
        if missing:
            raise ValueError(f"the allowable cost needs {', '.join(sorted(missing))}")
        return self


def guessed_stages(answers: OwnerAnswers) -> list[dict]:
    out = []
    for name, label in STAGES:
        r = answers.rates.get(name)
        if r is None:
            continue
        out.append({"name": name, "label": label, "rate": str(r.rate),
                    "low": str(r.low if r.low is not None else r.rate),
                    "high": str(r.high if r.high is not None else r.rate),
                    "tag": RateTag.GUESSED.value, "outcomes": 0})
    return out


def measure(stage: dict, successes: int, trials: int) -> dict:
    """Replace a guessed rate with a measured one once `trials` reaches MEASURED_AFTER. Below
    that the stage keeps its guess and records how many outcomes exist so far."""
    if successes < 0 or trials < 0 or successes > trials:
        raise ValueError("successes must be between 0 and trials")
    if trials < MEASURED_AFTER:
        return {**stage, "outcomes": trials}
    rate = (Decimal(successes) / Decimal(trials)).quantize(Decimal("0.0001"), ROUND_HALF_UP)
    return {**stage, "rate": str(rate), "low": str(rate), "high": str(rate),
            "tag": RateTag.MEASURED.value, "outcomes": trials}


def _rate(stages: list[dict], name: str, field: str = "rate") -> Decimal:
    for s in stages:
        if s["name"] == name:
            return Decimal(s[field])
    raise KeyError(name)


def allowable_cost(margin_per_won: Decimal, show_rate: Decimal, close_rate: Decimal,
                   share: Decimal = DEFAULT_SHARE) -> Decimal:
    return (Decimal(margin_per_won) * show_rate * close_rate * share).quantize(CENT, ROUND_HALF_UP)


def summary(stages: list[dict], margin_per_won: Decimal, share: Decimal, currency: str) -> dict:
    """The allowable cost with its range, the channel targets and the stopping rule. Labelled
    guessed while any rate it uses is guessed."""
    def at(field: str) -> Decimal:
        return allowable_cost(margin_per_won, _rate(stages, "booked_to_held", field),
                              _rate(stages, "held_to_won", field), share)

    a, lo, hi = at("rate"), at("low"), at("high")
    used = [s for s in stages if s["name"] in REQUIRED]
    tag = RateTag.MEASURED.value if all(s["tag"] == RateTag.MEASURED.value for s in used) else RateTag.GUESSED.value
    return {
        "currency": currency,
        "allowable_cost": str(a), "allowable_low": str(lo), "allowable_high": str(hi),
        "tag": tag,
        "formula": "margin per won deal x show rate x close rate x share of margin",
        "channel_targets": [{"role": k, "label": label, "multiple": str(m),
                             "max_cost": str((a * m).quantize(CENT, ROUND_HALF_UP)), "tag": RateTag.GUESSED.value}
                            for k, m, label in CHANNEL_TARGETS],
        "stop_after_spend": str((a * STOP_MULTIPLE).quantize(CENT, ROUND_HALF_UP)),
        "stop_rule": "Propose a pause when a campaign has spent 3 x A with no booked conversation.",
    }


def bottleneck(play_bottlenecks: tuple[str, ...]) -> str:
    """The first bottleneck to test, from the play's ordered list. Not a measurement."""
    return f"First to test (from the play, not measured): {play_bottlenecks[0]}"


__all__ = ["CHANNEL_TARGETS", "DEFAULT_SHARE", "MEASURED_AFTER", "STAGES", "OwnerAnswers", "Rate",
           "allowable_cost", "bottleneck", "guessed_stages", "measure", "summary"]
