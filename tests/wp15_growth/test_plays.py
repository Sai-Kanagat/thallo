"""The play library: data validates, shares are exact, fit rules and routes hold together."""
from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from thallo.contracts.enums import Channel, FunnelStage, LeadRoute
from thallo.growth.plays import DATA, MIN_PILLAR_SHARE, fit_range, library, load_text, parse


def _raw() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"), parse_float=Decimal)


def _with(code: str, **changes) -> dict:  # noqa: ANN003
    raw = copy.deepcopy(_raw())
    play = next(p for p in raw["plays"] if p["code"] == code)
    play.update(changes)
    return raw


def test_library_has_a_b_and_c():
    lib = library()
    assert [p.code for p in lib.plays] == ["A", "B", "C"]
    assert lib.get("C").status == "designed_not_piloted"


def test_draft_plays_match_the_lead_draft():
    draft = json.loads((DATA.parents[2] / "docs/plays/plays_draft.json").read_text(), parse_float=Decimal)
    for d in draft["plays"]:
        p = library().get(d["code"])
        assert p.pillar_mix == d["pillar_mix"]
        assert [c.value for c in p.channels] == d["channels"]
        assert len(p.fit_rules) == len(d["fit_rules"])


@pytest.mark.parametrize("code", ["A", "B", "C"])
def test_shares_are_exact(code):
    p = library().get(code)
    assert sum(p.pillar_mix.values()) == Decimal(1)
    assert all(v >= MIN_PILLAR_SHARE for v in p.pillar_mix.values())
    assert set(p.stage_mix) == set(FunnelStage)
    assert sum(p.stage_mix.values()) == Decimal(1)
    assert all(isinstance(c, Channel) for c in p.channels)


def test_pillar_share_must_sum_to_one():
    raw = _with("A", pillar_mix={"a_pillar": Decimal("0.5"), "b_pillar": Decimal("0.4")})
    with pytest.raises(ValidationError, match="sum to exactly 1"):
        parse(raw)


def test_pillar_share_floor():
    raw = _with("A", pillar_mix={"a_pillar": Decimal("0.95"), "b_pillar": Decimal("0.05")})
    with pytest.raises(ValidationError, match="between 0.10 and 1"):
        parse(raw)


def test_unknown_channel_refused():
    raw = _with("A", channels=["tiktok"])
    with pytest.raises(ValidationError):
        parse(raw)


def test_fit_rule_must_name_a_question_value():
    p = _raw()["plays"][0]
    raw = _with("A", fit_rules=[*p["fit_rules"], {"if": {"timing": "next_year"}, "add": 5}])
    with pytest.raises(ValidationError, match="cannot produce"):
        parse(raw)
    raw = _with("A", fit_rules=[*p["fit_rules"], {"if": {"budget": "high"}, "add": 5}])
    with pytest.raises(ValidationError, match="neither a question"):
        parse(raw)


def test_fit_rule_repeats_only_if_condition():
    p = _raw()["plays"][0]
    raw = _with("A", fit_rules=[*p["fit_rules"], {"if": {"down_payment_ready": "yes"}, "add": 5}])
    with pytest.raises(ValidationError, match="must also require payment=finance"):
        parse(raw)


def test_fit_must_stay_in_range():
    p = _raw()["plays"][0]
    raw = _with("A", fit_rules=[*p["fit_rules"], {"if": {"town": "in_service_area"}, "add": 40}])
    with pytest.raises(ValidationError, match="inside 0..100"):
        parse(raw)


def test_routes_must_partition():
    raw = _with("A", routes={"hot": {"fit_min": 60, "heat_min": 30}, "warm": {"fit_min": 50, "heat_min": 0},
                             "low_fit": {"fit_max": 59}})
    with pytest.raises(ValidationError, match="share one fit threshold"):
        parse(raw)
    raw = _with("A", routes={"hot": {"fit_min": 60, "heat_min": 30}, "warm": {"fit_min": 60, "heat_min": 0},
                             "low_fit": {"fit_max": 55}})
    with pytest.raises(ValidationError, match="one point below"):
        parse(raw)


@pytest.mark.parametrize("code", ["A", "B", "C"])
def test_every_fit_and_heat_lands_in_exactly_one_route(code):
    p = library().get(code)
    lo, hi = fit_range(p)
    assert hi >= p.routes.fit_threshold > lo
    seen = set()
    for fit in range(0, 101):
        for heat in range(0, 101):
            seen.add(p.routes.route(fit, heat))
    assert seen == set(LeadRoute)


def test_play_a_scoring_examples():
    a = library().get("A")
    hot = {"timing": "this_week", "payment": "cash", "town": "in_service_area", "model": "in_stock"}
    assert a.fit(hot) == 100
    assert a.route(hot, heat=40) is LeadRoute.HOT
    assert a.route(hot, heat=10) is LeadRoute.WARM
    cold = {"timing": "later", "payment": "finance", "down_payment_ready": "no"}
    assert a.fit(cold) == 0
    assert a.route(cold, heat=90) is LeadRoute.LOW_FIT


def test_play_b_uses_the_email_domain_fact():
    b = library().get("B")
    base = {"use_case": "matches_supported_list", "timing": "this_quarter"}
    assert b.fit({**base, "email_domain": "company"}) - b.fit(base) == 10


def test_floats_would_not_be_exact_but_parse_uses_decimal():
    lib = load_text(DATA.read_text(encoding="utf-8"))
    assert all(isinstance(v, Decimal) for p in lib.plays for v in p.pillar_mix.values())
