import pytest

from cortex_operator.contracts.enums import Language
from cortex_operator.truth import ClaimLedger, segment


def texts(text, language=Language.EN):
    return [s.text for s in segment(text, language)]


def test_build_plan_bug_decimal_with_reference_is_one_sentence():
    assert texts("3.5 tonnes [[C-0001]].") == ["3.5 tonnes [[C-0001]]."]
    assert texts("It carries 3.5 tonnes [[C-0001]]. Call us today.") == [
        "It carries 3.5 tonnes [[C-0001]].", "Call us today."]


@pytest.mark.parametrize("text", ["Capacity 3,5 tonnellate.", "Load: 1,234.50 kg.", "Visit example.com now.",
                                  "Shop at S.p.A. Rossi now."])
def test_decimals_urls_and_internal_periods_never_split(text):
    assert len(texts(text, Language.IT)) == 1


def test_english_abbreviations_and_lowercase_continuation():
    assert texts("Mr. Rossi came, e.g. on foot, approx. at noon. He left.") == [
        "Mr. Rossi came, e.g. on foot, approx. at noon.", "He left."]
    assert texts("It is fast. and cheap.") == ["It is fast. and cheap."]


def test_italian_abbreviations():
    assert texts("Il Dott. Bianchi ha detto sì, ecc. Poi è uscito.", Language.IT) == [
        "Il Dott. Bianchi ha detto sì, ecc. Poi è uscito."]
    assert texts("La Sig.ra Verdi è arrivata. Poi è uscita.", Language.IT) == [
        "La Sig.ra Verdi è arrivata.", "Poi è uscita."]


def test_tamil_titles_initials_and_units():
    assert texts("திரு. குமார் வந்தார். அவர் சென்றார்.", Language.TA) == [
        "திரு. குமார் வந்தார்.", "அவர் சென்றார்."]
    assert texts("மு. கருணாநிதி 3.5 கி.மீ. நடந்தார். சரி", Language.TA) == [
        "மு. கருணாநிதி 3.5 கி.மீ. நடந்தார்.", "சரி"]


def test_line_breaks_quotes_runs_and_trailing_markers():
    assert texts("New model\nBook a ride!") == ["New model", "Book a ride!"]
    assert texts('He said "yes." Then left.') == ['He said "yes."', "Then left."]
    assert texts("Really?! Yes... We open soon.") == ["Really?!", "Yes...", "We open soon."]
    assert texts("Fast delivery. {{C-0001}}\nNext") == ["Fast delivery. {{C-0001}}", "Next"]


def test_offsets_point_into_the_original_text():
    text = "  First one.   Second one.  "
    for s in segment(text):
        assert text[s.start:s.end] == s.text


def test_legacy_ledger_no_longer_flags_the_decimal_fragment():
    from cortex_operator.contracts.clock import FAKE_EPOCH
    ledger = ClaimLedger()
    claim = ledger.add("Payload 3.5 tonnes", "brochure", "brochure page 2")
    ledger.verify(claim.id)
    assert ledger.compile(f"It carries 3.5 tonnes [[{claim.id}]].", FAKE_EPOCH) == ([claim.id], [])
    assert ledger.compile("It carries 3.5 tonnes.", FAKE_EPOCH)[1]
