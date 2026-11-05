from datetime import datetime

from cortex_operator.contracts import Item
from cortex_operator.crew import CRITIC, STRATEGIST, CrewWriter
from cortex_operator.truth import ClaimLedger

NOW = datetime(2026, 11, 9)
ITEM = Item("a", "s", "linkedin", "Launch", NOW)


def scripted(drafts, reviews):
    calls = []

    def complete(system, prompt):
        calls.append((system, prompt))
        if system == STRATEGIST:
            return "Angle."
        return reviews.pop(0) if system == CRITIC else drafts.pop(0)
    return complete, calls


def test_clean_draft_takes_three_calls():
    complete, calls = scripted(["Good post."], ["PASS"])
    assert CrewWriter(complete, ClaimLedger(), clock=lambda: NOW).draft(ITEM, []) == "Good post."
    assert len(calls) == 3


def test_invented_number_is_rewritten_even_if_critic_passes():
    complete, calls = scripted(["We grew 300%.", "We grew steadily."], ["PASS", "PASS"])
    assert CrewWriter(complete, ClaimLedger(), clock=lambda: NOW).draft(ITEM, []) == "We grew steadily."
    assert "unsupported" in calls[3][1]


def test_rounds_are_capped():
    complete, calls = scripted(["a", "b", "c"], ["Fix it", "Fix it"])
    assert CrewWriter(complete, ClaimLedger(), clock=lambda: NOW).draft(ITEM, []) == "c"
    assert len(calls) == 6
