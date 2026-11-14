"""The segmenter is for display only. These tests pin its behaviour for writer feedback."""
import pytest

from thallo.contracts.enums import Language
from thallo.truth import segment


def texts(text, language=Language.EN):
    return [s.text for s in segment(text, language)]


def test_build_plan_bug_decimal_with_reference_is_one_sentence():
    assert texts("3.5 tonnes [[C-0001]].") == ["3.5 tonnes [[C-0001]]."]
    assert texts("It carries 3.5 tonnes [[C-0001]]. Call us today.") == [
        "It carries 3.5 tonnes [[C-0001]].", "Call us today."]


@pytest.mark.parametrize("text", ["Capacity 3.5 tonnes.", "Visit example.com now.", "Tools, e.g.spanners."])
def test_period_between_lowercase_alphanumerics_holds(text):
    assert len(texts(text)) == 1


@pytest.mark.parametrize(("text", "out"), [
    ("Best in Italy.Price 4.5 lakh {{C-0001}}.", ["Best in Italy.", "Price 4.5 lakh {{C-0001}}."]),
    ("Mr. Rossi came.", ["Mr.", "Rossi came."]),
    ("Ditta S.p.A. di Bologna.", ["Ditta S.", "p.", "A.", "di Bologna."]),
    ("Tools, e.g. spanners.", ["Tools, e.g.", "spanners."]),
    ("Really?! Yes... We open soon.", ["Really?!", "Yes...", "We open soon."]),
    ('He said "yes." Then left.', ['He said "yes."', "Then left."]),
])
def test_everything_else_splits(text, out):
    assert texts(text) == out


@pytest.mark.parametrize("brk", ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
def test_every_unicode_line_boundary_breaks(brk):
    assert texts(f"We grew fast{brk}Call us") == ["We grew fast", "Call us"]
    assert len(f"a{brk}b".splitlines()) == 2  # same set as str.splitlines


def test_markers_join_only_on_the_same_line():
    assert texts("Fast delivery. {{C-0001}}\nNext") == ["Fast delivery. {{C-0001}}", "Next"]
    assert texts("We grew 40%\n[[C-0001]]") == ["We grew 40%", "[[C-0001]]"]
    assert texts("We grew 40%\u2028[[C-0001]]") == ["We grew 40%", "[[C-0001]]"]


def test_tamil_segments():
    assert texts("திரு. குமார் வந்தார். அவர் சென்றார்.", Language.TA) == [
        "திரு.", "குமார் வந்தார்.", "அவர் சென்றார்."]
    assert texts("தூரம் 3.5 கி.மீ. மட்டும்", Language.TA) == ["தூரம் 3.5 கி.", "மீ.", "மட்டும்"]


def test_offsets_point_into_the_original_text():
    text = "  First one.   Second one.  "
    for s in segment(text):
        assert text[s.start:s.end] == s.text
