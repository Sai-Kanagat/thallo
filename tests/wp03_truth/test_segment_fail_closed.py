"""Segmentation is not a security boundary.

The gate decides from spans alone. These tests swap the segmenter for "whole text as one
sentence" and for "split at every character" and check that no verdict changes, for a corpus
that includes every trick reported in review. They also pin the legacy ledger: a [[ref]] supports
only itself, never a neighbouring sentence."""
import random

import pytest

from cortex_operator.contracts.clock import FAKE_EPOCH
from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.truth import ClaimLedger, compiler, legacy
from cortex_operator.truth.segment import Sentence

CORPUS = [
    "Best in Italy.Price 4.5 lakh {{C-0001}}.",
    "Best in Italy.Price 4.5 lakh [[C-0001]].",
    "We grew 40%\n[[C-0001]]",
    "We grew 40%\u2028[[C-0001]]", "We grew 40%\x85[[C-0001]]", "We grew 40%\v[[C-0001]]",
    "We grew 40%\f[[C-0001]]",
    "Tools, parts etc. We sold 40 [[C-0001]].",
    "Model no. 7 ships [[C-0001]].",
    "Mail info@example.it.We sold 40 [[C-0001]].",
    "Visit https://example.com/a.Best price [[C-0001]].",
    "The answer is no. Best price [[C-0001]].",
    "Plan B. Best price {{C-0001}}.",
    "Mr. Rossi came. He sold {{C-0001}}.",
    "It carries {{C-0001}}. Call us today.",
    "Carries {{C-0001}} every day.",
    "Ditta S.p.A. il migliore {{C-0001}}.",
    "திரு. குமார் சிறந்த டீலர் {{C-0001}}.",
    "Plain copy with nothing to check.",
    "{{C-0001}}{{C-0002}} trucks.",
    "Trusted by Rossi Trasporti. {{C-0001}}",
]


def whole(text, language=Language.EN):
    return [Sentence(0, 0, len(text), text)] if text else []


def every_char(text, language=Language.EN):
    return [Sentence(i, i, i + 1, ch) for i, ch in enumerate(text)]


@pytest.fixture
def library(make):
    return {c.code: c for c in (make("C-0001", ClaimKind.NUMBER, "10"), make("C-0002", ClaimKind.NUMBER, "5"),
                                make("C-0003", ClaimKind.NAME, "Rossi Trasporti"))}


def _verdicts(run, library, language):
    out = []
    for text in CORPUS:
        r = run(text, language, claims=library)
        out.append((text, r.ok, sorted((f.kind.value, f.start, f.end) for f in r.findings)))
    return out


def _legacy_verdicts():
    led = ClaimLedger()
    cid = led.add("fact", "brochure", "page 1").id
    led.verify(cid)
    out = []
    for text in CORPUS:
        ids, errors = led.compile(text, FAKE_EPOCH)
        out.append((text, ids, bool(errors)))
    return out


@pytest.mark.parametrize("language", [Language.EN, Language.IT, Language.TA])
def test_compile_verdict_does_not_depend_on_segmentation(run, library, monkeypatch, language):
    real = _verdicts(run, library, language)
    for fake in (whole, every_char):
        monkeypatch.setattr(compiler, "_segment", fake)
        assert _verdicts(run, library, language) == real, fake.__name__


def test_legacy_verdict_does_not_depend_on_segmentation(monkeypatch):
    real = _legacy_verdicts()
    for fake in (whole, every_char):
        monkeypatch.setattr(legacy, "_segment", fake)
        assert _legacy_verdicts() == real, fake.__name__


def test_every_corpus_trick_with_unmarked_checkable_text_is_blocked(run, library):
    for text in CORPUS:
        if text in ("It carries {{C-0001}}. Call us today.", "Carries {{C-0001}} every day.",
                    "Plain copy with nothing to check.", "Mr. Rossi came. He sold {{C-0001}}."):
            assert run(text, claims=library).ok, text
        else:
            assert not run(text, claims=library).ok, text


# legacy ledger: a reference supports only itself

def ledger():
    led = ClaimLedger()
    claim = led.add("fact", "brochure", "page 1")
    led.verify(claim.id)
    return led, claim.id


def test_legacy_reference_never_covers_digits_or_superlatives_beside_it():
    led, cid = ledger()
    for text in (f"We grew 40% [[{cid}]].", f"Best in Italy.Price [[{cid}]].", f"We grew 40%\n[[{cid}]]",
                 f"Tools etc. We sold 40 [[{cid}]].", f"The best dealer [[{cid}]]."):
        ids, errors = led.compile(text, FAKE_EPOCH)
        assert ids == [cid] and errors and errors[0].startswith("unsupported:"), text


def test_legacy_clean_text_and_reference_problems():
    led, cid = ledger()
    assert led.compile(f"Calm weeks [[{cid}]].", FAKE_EPOCH) == ([cid], [])
    assert led.compile("Calm weeks [[C-0099]].", FAKE_EPOCH)[1] == ["C-0099 does not exist"]
    ids, errors = led.compile("Payload 3.5 tonnes.", FAKE_EPOCH)
    assert errors == ["unsupported: Payload 3.5 tonnes. (digit '3', digit '5')"]


# property: whatever the joins, unmarked digits are always flagged, by both gates

JOINS = [" ", "", "\n", "  ", " \n", "\n\n", "\u2028", "\x85", "\v", "\f"]
ENDS = [".", "!", "?", "...", " etc.", " co.", " no.", " B.", ".)", '."']
STARTS = ["We", "Since", "Price", "Best", "Il", "Over", "Our"]


@pytest.mark.parametrize("seed", range(300))
def test_property_unmarked_digits_are_always_flagged(seed, run):
    rng = random.Random(seed)
    led, cid = ledger()
    a = f"{rng.choice(STARTS)} sold {rng.randint(1, 999)}.{rng.randint(0, 9)} units{rng.choice(ENDS)}"
    b = f"{rng.choice(STARTS)} grew {rng.randint(1, 99)}%"
    if rng.random() < 0.5:
        text = f"{a} [[{cid}]]{rng.choice(JOINS)}{b}{rng.choice(['.', '', '!'])}"
    else:
        text = f"{a}{rng.choice(JOINS)}{b} [[{cid}]]{rng.choice(['.', '', '!'])}"
    errors = led.compile(text, FAKE_EPOCH)[1]
    assert errors and all(e.startswith("unsupported:") for e in errors), (text, errors)
    flagged = " ".join(errors)
    assert "digit" in flagged and "symbol '%'" in flagged, (text, errors)

    result = run(text.replace(f"[[{cid}]]", ""))
    digit_spans = [(f.start, f.end) for f in result.findings if f.kind.value == "digit"]
    for i, ch in enumerate(result.text):
        if ch.isdigit():
            assert any(x <= i < y for x, y in digit_spans), (text, i)
