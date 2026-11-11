"""Security review findings: ambiguous segmentation must produce MORE fragments, never fewer, so
every checkable token needs its own marker."""
import random

import pytest

from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.enums import Language
from cortex_operator.truth import ClaimLedger, compile_text, segment


def texts(text, language=Language.EN):
    return [s.text for s in segment(text, language)]


def ledger():
    led = ClaimLedger()
    claim = led.add("fact", "brochure", "page 1")
    led.verify(claim.id)
    return led, claim.id


# 1. terminator followed directly by a non-space

def test_no_whitespace_after_terminator_still_splits():
    assert texts("Best in Italy.Price 4.5 lakh {{C-0001}}.") == ["Best in Italy.", "Price 4.5 lakh {{C-0001}}."]
    assert texts("We sold 40.Then [[C-0001]].") == ["We sold 40.", "Then [[C-0001]]."]
    assert texts("Fast!Cheap [[C-0001]].") == ["Fast!", "Cheap [[C-0001]]."]
    assert texts("Top.top") == ["Top.", "top"]


def test_no_whitespace_merge_is_caught_by_the_legacy_gate():
    led, cid = ledger()
    errors = led.compile(f"Best in Italy.Price 4.5 lakh [[{cid}]].", FAKE_EPOCH)[1]
    assert errors == ["unsupported: Best in Italy."]


@pytest.mark.parametrize("text", ["Load 3.5 tonnes.", "Visit https://example.com/a.b today.",
                                  "Mail info@example.it now.", "See example.com now."])
def test_only_decimals_urls_and_emails_hold_together(text):
    assert len(texts(text)) == 1


# 2. marker-only fragment across a line break

def test_marker_on_next_line_does_not_join_previous_sentence():
    assert texts("We grew 40%\n[[C-0001]]") == ["We grew 40%", "[[C-0001]]"]
    assert texts("We grew 40%.\n{{C-0001}}") == ["We grew 40%.", "{{C-0001}}"]
    assert texts("We grew 40%. [[C-0001]]") == ["We grew 40%. [[C-0001]]"]


def test_marker_across_line_break_is_caught_by_the_legacy_gate():
    led, cid = ledger()
    assert led.compile(f"We grew 40%\n[[{cid}]]", FAKE_EPOCH)[1] == ["unsupported: We grew 40%"]


# 3. abbreviations and initials

def test_abbreviation_followed_by_uppercase_or_new_sentence_splits():
    assert texts("We are the best co. Since then 40 trucks [[C-0001]].") == [
        "We are the best co.", "Since then 40 trucks [[C-0001]]."]
    assert texts("Tools, parts etc. We sold 40 [[C-0001]].") == ["Tools, parts etc.", "We sold 40 [[C-0001]]."]
    assert texts("The answer is no. Best price [[C-0001]].") == ["The answer is no.", "Best price [[C-0001]]."]
    assert texts("Plan B. Best price [[C-0001]].") == ["Plan B.", "Best price [[C-0001]]."]


def test_abbreviation_followed_by_lowercase_or_digit_holds():
    assert texts("Tools, e.g. spanners, approx. ten. Done.") == ["Tools, e.g. spanners, approx. ten.", "Done."]
    assert texts("Model no. 7 ships.") == ["Model no. 7 ships."]


def test_abbreviation_merge_is_caught_by_the_legacy_gate():
    led, cid = ledger()
    errors = led.compile(f"Tools, parts etc. We sold 40 trucks. Best price [[{cid}]].", FAKE_EPOCH)[1]
    assert errors == ["unsupported: We sold 40 trucks."]


# property: two sentences with digits, one marked, the unmarked one is always flagged

JOINS = [" ", "", "\n", "  ", " \n", "\n\n"]
ENDS = [".", "!", "?", "...", " etc.", " co.", " no.", " B.", ".)", '."']
STARTS = ["We", "Since", "Price", "Best", "Il", "Over", "Our"]


@pytest.mark.parametrize("seed", range(300))
def test_property_unmarked_digit_sentence_is_always_flagged(seed):
    rng = random.Random(seed)
    led, cid = ledger()
    a = f"{rng.choice(STARTS)} sold {rng.randint(1, 999)}.{rng.randint(0, 9)} units{rng.choice(ENDS)}"
    b = f"{rng.choice(STARTS)} grew {rng.randint(1, 99)}%"
    marked_first = rng.random() < 0.5
    tail = rng.choice([".", "", "!"])
    if marked_first:
        text = f"{a[:-1] if a.endswith('.') else a} [[{cid}]].{rng.choice(JOINS)}{b}{tail}"
        unmarked = b
    else:
        text = f"{a}{rng.choice(JOINS)}{b} [[{cid}]]{tail}"
        unmarked = a
    errors = led.compile(text, FAKE_EPOCH)[1]
    assert any(e.startswith("unsupported:") for e in errors), (text, errors)
    assert not any(cid in e for e in errors), (text, errors)
    flagged = " ".join(errors)
    digits = [w for w in unmarked.split() if any(c.isdigit() for c in w)]
    assert any(d.rstrip('.!?")') in flagged for d in digits), (text, errors)

    # the slot-based compile step flags the unmarked digits whatever the segmentation
    slot_text = text.replace(f"[[{cid}]]", "{{C-0001}}")
    result = compile_text(slot_text, language=Language.EN, claims={}, clock=FakeClock(FAKE_EPOCH))
    assert any(f.kind.value == "digit" for f in result.findings)
