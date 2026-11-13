"""Every probe string from the independent review (probe.py, probe2.py) must fail the build."""
import pytest

from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.truth import CompileFailed, FillError, FindingKind
from cortex_operator.truth.compiler import check_filled
from cortex_operator.truth.fill import Filled

EN, IT, TA = Language.EN, Language.IT, Language.TA


def bare(value):
    return {"en": value, "it": value, "ta": value}


@pytest.fixture
def library(make):
    return {c.code: c for c in (make("C-0001", ClaimKind.NUMBER, "10", languages=bare("10")),
                                make("C-0002", ClaimKind.NUMBER, "5", languages=bare("5")),
                                make("C-0003", ClaimKind.NAME, "Rossi Trasporti"))}


def kinds(result):
    return {f.kind for f in result.findings}


# B1: number words, compounds, ordinals, Roman numerals, multipliers
B1 = [
    (IT, "Trentacinque clienti."), (IT, "Ventitré anni."), (IT, "Duemilacinquecento euro."),
    (IT, "Tredici sedi."), (IT, "Quindici anni."), (IT, "Centoventi clienti."), (IT, "Quarantadue sedi."),
    (IT, "Migliaia di clienti."), (IT, "Centinaia di clienti."), (IT, "Sei anni di garanzia."),
    (IT, "Il secondo anno."), (IT, "Il terzo anno."),
    (IT, "La prima concessionaria."), (IT, "Cinquanta per cento."), (IT, "Mezzo milione."),
    (IT, "Un anno record."), (IT, "Una decina di sedi."),
    (TA, "பதினைந்து ஆண்டுகள்."), (TA, "பதினொன்று கடைகள்."), (TA, "இருநூறு வாடிக்கையாளர்கள்."),
    (TA, "எழுபது ஆண்டுகள்."), (TA, "ஆயிரம் வாடிக்கையாளர்கள்."), (TA, "ஆயிரக்கணக்கான வாடிக்கையாளர்கள்."),
    (EN, "One hundred customers."), (EN, "We sold one truck."), (EN, "Our second year."),
    (EN, "Third year running."), (EN, "A fivefold increase."),
    (EN, "A record year."), (EN, "Our bestseller model."), (EN, "Serving you for XV years."),
    (EN, "Over xii years."), (EN, "Serving you for MCMXC years."), (EN, "Fifty percent off."),
    (EN, "Two per cent less."), (EN, "Sales of mln units."),
]


@pytest.mark.parametrize(("language", "text"), B1)
def test_b1_number_words_block(run, language, text):
    result = run(text, language)
    assert not result.ok, text


def test_b1_top_flags_after_the_precision_ruling(run):
    for language, text in ((IT, "Il top in Emilia."), (EN, "The top dealer in Chennai.")):
        result = run(text, language)
        assert result.ok and result.flags, text


def test_b1_roman_numeral_lookalike_words_pass(run):
    for text in ("I mix it, dim the lights.", "Di mi vi ci li."):
        assert FindingKind.NUMBER_WORD not in kinds(run(text, IT if text.startswith("Di") else EN)), text


# B2: slots that would print a new number
B2 = ["{{C-0001}}{{C-0002}} trucks sold.", "{{C-0001}}.{{C-0002}} tonnes.", "{{C-0001}},{{C-0002}} tonnes.",
      "{{C-0001}}k customers served.", "{{C-0001}}M in sales.", "{{C-0001}} mln di fatturato.",
      "{{C-0001}}x the output.", "{{C-0001}} x the output.", "{{C-0001}}-{{C-0002}} days.",
      "{{C-0001}}/{{C-0002}} rating.", "{{C-0001}}:{{C-0002}} odds.", "{{C-0001}}'{{C-0002}} units.",
      "{{C-0001}}fold growth.", "{{C-0001}} fold growth.", "A{{C-0001}} model.", "Model {{C-0001}}b."]


@pytest.mark.parametrize("text", B2)
def test_b2_slot_that_could_print_a_new_number_blocks(run, library, text):
    result = run(text, IT if "mln" in text else EN, claims=library)
    assert FindingKind.SLOT_ADJACENT in kinds(result), (text, result.findings)


def test_b2_separate_slots_pass(run, built, library):
    assert run("{{C-0001}} trucks and {{C-0002}} vans.", claims=library).ok
    assert built("{{C-0001}} trucks and {{C-0002}} vans.", library) == "10 trucks and 5 vans."


def test_b2_post_fill_check_catches_a_digit_outside_every_slot():
    with pytest.raises(FillError, match="outside a slot"):
        check_filled(Filled(text="10 trucks, 5 vans", codes=("C-0001",), claim_ids=("x",), slot_spans=((0, 2),)))
    check_filled(Filled(text="10 trucks", codes=("C-0001",), claim_ids=("x",), slot_spans=((0, 2),)))


# B3: lookalikes and other scripts
B3 = [
    "Fullwidth: ｂｅｓｔ dealer in town.",  # fullwidth best
    "Math bold: \U0001d41b\U0001d41e\U0001d42c\U0001d42d dealer.",  # mathematical bold best
    "Ligature: the ﬁrst dealer.",  # fi ligature
    "Cyrillic e: bеst dealer.",
    "Greek or Cyrillic o: the оnly dealer.",
    "Long s: the beſt dealer.",
    "CGJ: the be\u034fst dealer.",
    "Variation selector: the bes\ufe00t dealer.",
    "Hangul filler: the be\u3164st dealer.",
    "Combining acute: the bést dealer.",
    "Small caps: the ʙᴇꜱᴛ dealer.",
    "Spaced: the b e s t dealer.",
    "Braille blank: the be\u2800st dealer.",
    "Supplementary selector: the bes\U000e0100t dealer.",
]


@pytest.mark.parametrize("text", B3)
def test_b3_lookalikes_and_hidden_characters_block(run, text):
    assert not run(text).ok, text


@pytest.mark.parametrize("name", ["Rоssi Trasporti", "Ｒｏｓｓｉ Trasporti",
                                  "Rossi-Trasporti", "ROSSI TRASPORTI", "Rossitrasporti"])
def test_b3_protected_name_lookalikes_block(run, library, name):
    assert not run(f"Trusted by {name}.", claims=library).ok, name


def test_b3_script_rules_per_language(run):
    assert FindingKind.FOREIGN_SCRIPT in kinds(run("சிறப்பு dealer."))  # Tamil letters in English copy
    assert FindingKind.FOREIGN_SCRIPT not in kinds(run("Dealer in சென்னை.", TA))
    assert FindingKind.FOREIGN_SCRIPT in kinds(run("Αθήνα dealer.", TA))  # Greek
    assert run("Caffè, città, perché.", IT).ok


# Build output from the probe set never carries an unverified number
def test_probe_build_raises_instead_of_printing(built, library):
    for text in ("{{C-0001}}{{C-0002}} trucks.", "Get {{C-0001}}k now."):
        with pytest.raises(CompileFailed):
            built(text, library)
