"""The ladder's pure rules: bounds, unlock conditions, the human-only refusal, canary spacing."""
from __future__ import annotations

import pytest

from thallo.contracts.enums import AutonomyLevel, HumanOnlyAction
from thallo.trust import ladder
from thallo.trust.ladder import L0, L1, L2, L3, L4, Evidence, eligibility


def test_bounds_match_the_spec_table():
    # BUILD_PLAN 2.8: exact one-sided 95% upper bounds on the true defect rate
    assert ladder.bound_pct(2, 50) == "12.1"
    assert ladder.bound_pct(1, 100) == "4.7"
    assert ladder.bound_pct(3, 200) == "3.8"
    assert ladder.bound_pct(0, 300) == "1.0"  # the rule of three
    assert ladder.defect_upper_bound(0, 0) == 1.0
    with pytest.raises(ValueError):
        ladder.defect_upper_bound(5, 3)


@pytest.mark.parametrize("level", list(AutonomyLevel))
@pytest.mark.parametrize("action", list(HumanOnlyAction))
def test_no_level_unlocks_a_human_only_action(action, level):
    assert ladder.system_may_do_alone(action, level) is False


def ev(**kw) -> Evidence:  # noqa: ANN003
    base = dict(level=L1, items=50, defects=0, escaped=0, hard_rule_hits=0)
    base.update(kw)
    return Evidence(**base)


def test_l0_to_l1_needs_calibration_and_signed_voice_rules():
    e = eligibility(ev(level=L0, items=20))
    assert not e.ok and any("calibration" in r for r in e.reasons) and any("Voice" in r for r in e.reasons)
    assert eligibility(ev(level=L0, items=20, calibration_done=True, voice_rules_signed=True)).ok


def test_l1_to_l2_bounds():
    assert eligibility(ev(items=50, defects=2)).ok
    assert eligibility(ev(items=50, defects=2)).bound_pct == "12.1"
    assert not eligibility(ev(items=49)).ok
    assert not eligibility(ev(items=50, defects=3)).ok
    assert not eligibility(ev(items=50, defects=1, escaped=1)).ok
    assert not eligibility(ev(items=50, hard_rule_hits=1)).ok
    assert not eligibility(ev(items=50, paused=True)).ok


def test_l2_to_l3_needs_measured_canaries():
    base = dict(level=L2, items=100, defects=1)
    assert not eligibility(ev(**base, canaries_shown=4, canaries_caught=4)).ok  # too few to say anything
    assert not eligibility(ev(**base, canaries_shown=5, canaries_caught=3)).ok  # 60%
    ok = eligibility(ev(**base, canaries_shown=5, canaries_caught=4))
    assert ok.ok and ok.target is L3 and "thin evidence" in ok.canary_note
    assert not eligibility(ev(**base | {"defects": 2}, canaries_shown=5, canaries_caught=5)).ok


def test_l3_to_l4_needs_a_signed_template_and_nothing_above():
    base = dict(level=L3, items=200, defects=3)
    assert not eligibility(ev(**base)).ok
    assert eligibility(ev(**base, template_signed=True)).ok
    top = eligibility(ev(level=L4, items=500))
    assert not top.ok and top.target is None


def test_version_change_must_be_reearned_with_30_clean():
    assert not eligibility(ev(version_reearn_needed=True, clean_since_version=29)).ok
    assert eligibility(ev(version_reearn_needed=True, clean_since_version=30)).ok


def test_demotion_steps():
    assert ladder.down_one(L3) is L2 and ladder.down_one(L0) is L0
    assert ladder.to_l1(L4) is L1 and ladder.to_l1(L0) is L0


@pytest.mark.parametrize("offset", [0, 7, 19])
def test_one_canary_in_twenty_deterministic(offset):
    picks = [s for s in range(400) if ladder.plant_canary_now(s, offset)]
    assert len(picks) == 20
    assert all(b - a == 20 for a, b in zip(picks, picks[1:], strict=False))
