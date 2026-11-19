"""Growth model maths and tags: allowable cost pinned to the plan's worked example, guessed until
30 outcomes, and a policy check that fails closed."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from thallo.contracts.clock import FakeClock
from thallo.contracts.enums import RateTag
from thallo.contracts.ids import new_id
from thallo.contracts.models import RegionPolicy
from thallo.growth import model as gm
from thallo.growth.plays import library
from thallo.growth.policy import PolicyStatus, check_play, current_policy

NOW = datetime(2026, 11, 19, tzinfo=UTC)
CLOCK = FakeClock(NOW)


def _answers(**kw) -> gm.OwnerAnswers:  # noqa: ANN003
    d = {"play_code": "A", "margin_per_won": "600", "rates": {"booked_to_held": {"rate": "0.70"},
                                                             "held_to_won": {"rate": "0.25"}}}
    d.update(kw)
    return gm.OwnerAnswers.model_validate(d)


def test_worked_example_from_the_plan():
    assert gm.allowable_cost(Decimal(600), Decimal("0.70"), Decimal("0.25")) == Decimal("31.50")


def test_form_rates_are_always_guessed():
    stages = gm.guessed_stages(_answers())
    assert {s["tag"] for s in stages} == {RateTag.GUESSED.value}
    s = gm.summary(stages, Decimal(600), Decimal("0.30"), "EUR")
    assert s["allowable_cost"] == "31.50" and s["tag"] == "guessed" and s["currency"] == "EUR"
    assert s["stop_after_spend"] == "94.50"
    assert all(t["tag"] == "guessed" for t in s["channel_targets"])


def test_form_cannot_send_a_tag():
    with pytest.raises(ValidationError):
        gm.OwnerAnswers.model_validate({"play_code": "A", "margin_per_won": "600", "rates": {
            "booked_to_held": {"rate": "0.7", "tag": "measured"}, "held_to_won": {"rate": "0.25"}}})


def test_measured_needs_thirty_outcomes():
    stage = gm.guessed_stages(_answers())[0]
    below = gm.measure(stage, successes=20, trials=29)
    assert below["tag"] == "guessed" and below["rate"] == "0.70" and below["outcomes"] == 29
    at = gm.measure(stage, successes=21, trials=30)
    assert at["tag"] == "measured" and at["rate"] == "0.7000" and at["outcomes"] == 30


def test_summary_stays_guessed_while_any_used_rate_is_guessed():
    stages = gm.guessed_stages(_answers())
    stages[0] = gm.measure(stages[0], 21, 30)
    assert gm.summary(stages, Decimal(600), Decimal("0.3"), "EUR")["tag"] == "guessed"
    stages[1] = gm.measure(stages[1], 10, 40)
    assert gm.summary(stages, Decimal(600), Decimal("0.3"), "EUR")["tag"] == "measured"


def test_ranges_give_a_range_of_allowable_cost():
    a = _answers(rates={"booked_to_held": {"rate": "0.7", "low": "0.5", "high": "0.8"},
                        "held_to_won": {"rate": "0.25", "low": "0.2", "high": "0.3"}})
    s = gm.summary(gm.guessed_stages(a), Decimal(600), Decimal("0.3"), "INR")
    assert Decimal(s["allowable_low"]) < Decimal(s["allowable_cost"]) < Decimal(s["allowable_high"])


def test_answers_need_show_and_close_rates():
    with pytest.raises(ValidationError, match="needs"):
        _answers(rates={"booked_to_held": {"rate": "0.7"}})
    with pytest.raises(ValidationError, match="low <= rate <= high"):
        _answers(rates={"booked_to_held": {"rate": "0.7", "low": "0.8"}, "held_to_won": {"rate": "0.25"}})


def test_bottleneck_is_labelled_not_measured():
    label = gm.bottleneck(library().get("A").bottlenecks_in_order)
    assert label.startswith("First to test (from the play, not measured)")


def _policy(rules: dict, version: int = 1, start: datetime = NOW - timedelta(days=1)) -> RegionPolicy:
    return RegionPolicy(id=new_id(CLOCK), tenant_id=new_id(CLOCK),
                        created_at=NOW, region_code="IT", version=version, rules=rules, effective_from=start)


def test_policy_missing_is_not_a_pass():
    r = check_play(library().get("A"), None, "IT")
    assert r.status is PolicyStatus.NO_POLICY and r.problems


def test_policy_contradictions():
    a = library().get("A")
    r = check_play(a, _policy({"blocked_channels": ["whatsapp"], "blocked_question_keys": ["payment"],
                               "max_channels_live": 2, "lawyer_note": "x"}), "IT")
    assert r.status is PolicyStatus.CONTRADICTS
    assert len(r.problems) == 3 and r.not_checked == ["lawyer_note"]
    assert check_play(a, _policy({}), "IT").status is PolicyStatus.OK


def test_current_policy_picks_the_latest_live_version():
    rows = [_policy({}, 1), _policy({"blocked_channels": ["email"]}, 2),
            _policy({"blocked_channels": ["whatsapp"]}, 3, start=NOW + timedelta(days=5))]
    assert current_policy(rows, "IT", NOW).version == 2
    assert current_policy(rows, "IN", NOW) is None
