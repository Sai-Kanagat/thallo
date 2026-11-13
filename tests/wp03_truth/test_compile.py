import pytest

from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.contracts.ids import new_id
from cortex_operator.truth import CompileFailed, Finding, FindingKind, LibraryError
from tests.wp03_truth.conftest import DAY

EN, IT, TA = Language.EN, Language.IT, Language.TA


def kinds(result):
    return [f.kind for f in result.findings]


# ---------------------------------------------------------------- digits

@pytest.mark.parametrize("digit", [
    "7", "௭",  # Tamil digit seven
    "७",  # Devanagari seven
    "٧",  # Arabic-Indic seven
    "７",  # fullwidth seven
    "²",  # superscript two
    "½",  # vulgar fraction one half
    "Ⅻ",  # Roman numeral twelve
    "௱",  # Tamil number one hundred
])
def test_any_digit_outside_a_slot_fails(run, digit):
    assert FindingKind.DIGIT in kinds(run(f"We sold {digit} units."))


def test_decimal_outside_a_slot_is_flagged_with_its_span(run):
    result = run("Payload 3.5 tonnes. Call us.")
    digits = [f for f in result.findings if f.kind is FindingKind.DIGIT]
    assert [(f.match, f.sentence) for f in digits] == [("3", 0), ("5", 0)]
    assert result.text[digits[0].start:digits[0].end] == "3"


def test_currency_and_percent_signs_fail(run):
    assert FindingKind.SYMBOL in kinds(run("Save big in € today."))
    assert FindingKind.SYMBOL in kinds(run("Zero interest %."))


# ---------------------------------------------------------------- per-language lists

@pytest.mark.parametrize(("language", "text", "kind"), [
    (EN, "The best dealer in town.", FindingKind.SUPERLATIVE),
    (EN, "A world-class service.", FindingKind.SUPERLATIVE),
    (EN, "Cheaper than the rest.", FindingKind.COMPARATIVE),
    (EN, "Twelve showrooms.", FindingKind.NUMBER_WORD),
    (IT, "Il servizio migliore di Bologna.", FindingKind.SUPERLATIVE),
    (IT, "Prezzi convenientissimi.", FindingKind.SUPERLATIVE),
    (IT, "Il più veloce in città.", FindingKind.SUPERLATIVE),
    (IT, "Più veloce della concorrenza.", FindingKind.COMPARATIVE),
    (IT, "Meglio rispetto agli altri.", FindingKind.COMPARATIVE),
    (IT, "Garantito per sempre.", FindingKind.SUPERLATIVE),
    (IT, "Tre anni di garanzia.", FindingKind.NUMBER_WORD),
    (IT, "The best in Italia.", FindingKind.SUPERLATIVE),  # English inside Italian copy
    (TA, "சென்னையில் சிறந்த டீலர்.", FindingKind.SUPERLATIVE),
    (TA, "மிகக் குறைந்த விலை.", FindingKind.SUPERLATIVE),  # very + low
    (TA, "சென்னையின் முன்னணி டீலர்.", FindingKind.SUPERLATIVE),
    (TA, "மற்றவர்களை விட வேகமாக.", FindingKind.COMPARATIVE),
    (TA, "இதை விடக் குறைவு.", FindingKind.COMPARATIVE),
    (TA, "ஐந்து ஆண்டு உத்தரவாதம்.", FindingKind.NUMBER_WORD),
    (TA, "Best offer இன்று.", FindingKind.SUPERLATIVE),  # English inside Tamil copy
])
def test_per_language_lists(run, language, text, kind):
    result = run(text, language)
    assert kind in kinds(result), result.findings


@pytest.mark.parametrize(("language", "text"), [
    (EN, "Learn more about our three-wheelers. Book a test ride today."),
    (EN, "We would love to hear from you, so talk to someone on our team."),
    (IT, "Scopri di più sul nostro servizio. Nel rispetto dell'ambiente."),
    (IT, "Chiamaci, un consulente ti risponde."),
    (TA, "அதிகாரப்பூர்வ டீலர். விடுமுறை நாளிலும் திறந்திருக்கும்."),
    (TA, "இன்றே பதிவு செய்யுங்கள்."),  # registration, not the teens root
])
def test_ordinary_copy_passes(run, language, text):
    result = run(text, language)
    assert result.ok, result.findings


def test_ambiguous_italian_words_that_count_nothing_produce_nothing(run):
    """Review ruling L2: a preposition, verb or article that counts nothing is not a finding."""
    for text in ("Prima di partire, chiamaci. Sei pronto?", "Una bella giornata."):
        result = run(text, IT)
        assert result.ok and not result.flags, text


# ---------------------------------------------------------------- names, hidden characters, slots

def test_named_customer_outside_a_slot_fails_any_claim_state(run, make):
    name = make("C-0001", ClaimKind.NAME, "Rossi Trasporti", verified=False, languages={"ta": "ரோஸி"})
    claims = {name.code: name}
    assert FindingKind.NAMED_CUSTOMER in kinds(run("Ask rossi  trasporti how it went.", claims=claims))
    assert FindingKind.NAMED_CUSTOMER in kinds(run("Ask Rossi-Trasporti how it went.", claims=claims))
    assert FindingKind.NAMED_CUSTOMER in kinds(run("ரோஸி சொன்னது.", TA, claims=claims))
    assert FindingKind.NAMED_CUSTOMER in kinds(run("Ask Bianchi Srl.", protected_names=["Bianchi Srl"]))
    assert run("Ask Rossini.", claims=claims).ok


def test_hidden_character_cannot_split_a_word(run):
    assert FindingKind.HIDDEN_CHARACTER in kinds(run("The be\u200bst dealer."))


@pytest.mark.parametrize("text", ["Price {{C-12}}.", "Price {C-0001}.", "Price [[C-0001]].", "Price {{ C-0001 }}."])
def test_malformed_slots_fail(run, text):
    assert FindingKind.MALFORMED_SLOT in kinds(run(text))


def test_slots_need_live_verified_allowed_claims(run, make, clock):
    good = make("C-0001", ClaimKind.NUMBER, "3.5", "t")
    pending = make("C-0002", ClaimKind.NUMBER, "40", verified=False)
    short = make("C-0003", ClaimKind.PRICE, "9.99", "EUR", verify_expires_at=clock.now() + DAY)
    claims = {c.code: c for c in (good, pending, short)}

    ok = run("It carries {{C-0001}}. Simply honest.", claims=claims)
    assert ok.ok and ok.codes == ("C-0001",) and ok.claim_ids == (good.id,)
    assert kinds(run("{{C-0009}}", claims=claims)) == [FindingKind.UNKNOWN_CLAIM]
    assert kinds(run("{{C-0002}}", claims=claims)) == [FindingKind.UNUSABLE_CLAIM]
    assert kinds(run("{{C-0001}}", claims=claims, allowed_claim_ids=[short.id])) == [FindingKind.CLAIM_NOT_ALLOWED]
    assert run("{{C-0003}}", claims=claims).ok
    clock.advance(days=1)
    assert kinds(run("{{C-0003}}", claims=claims)) == [FindingKind.UNUSABLE_CLAIM]


def test_detector_hook_sees_masked_text_and_can_block(run, make):
    claims = {"C-0001": make("C-0001", ClaimKind.NAME, "Acme")}
    seen = []

    class Flagger:
        def detect(self, masked_text, sentences, language):
            seen.append(masked_text)
            i = masked_text.find("legendary")
            return [Finding(FindingKind.CLASSIFIER, i, i + 9, "legendary", "model: superlative")] if i >= 0 else []

    result = run("{{C-0001}} is legendary.", claims=claims, detectors=[Flagger()])
    assert kinds(result) == [FindingKind.CLASSIFIER]
    assert "{{" not in seen[0]


def test_build_compiles_then_fills_or_raises(built, make):
    claims = {"C-0001": make("C-0001", ClaimKind.NUMBER, "3.5", "t")}
    assert built("Porta {{C-0001}}.", claims, IT) == "Porta 3,5 tonnellate."
    with pytest.raises(CompileFailed) as err:
        built("Porta {{C-0001}}, il migliore.", claims, IT)
    assert err.value.result.findings[0].kind is FindingKind.SUPERLATIVE


# ---------------------------------------------------------------- wheelers

@pytest.mark.parametrize("text", ["We sold three wheelers.", "Three  wheelers in stock.", "two\nwheelers"])
def test_spaced_number_word_before_wheelers_stays_checkable(run, text):
    assert FindingKind.NUMBER_WORD in kinds(run(text))


@pytest.mark.parametrize("text", ["Our three-wheelers.", "A threewheeler.", "Two-wheeler service."])
def test_closed_or_hyphenated_wheeler_compound_is_a_category(run, text):
    assert run(text).ok


# ---------------------------------------------------------------- tenant and library (B4)

def test_claim_of_another_tenant_or_miskeyed_claim_is_refused(run, built, make, clock):
    other = make("C-0009", ClaimKind.PRICE, "99", "EUR", tenant_id=new_id(clock))
    with pytest.raises(LibraryError, match="another tenant"):
        run("Only {{C-0009}} today.", claims={"C-0009": other})
    with pytest.raises(LibraryError, match="another tenant"):
        built("Get {{C-0009}} today.", {"C-0009": other})
    mine = make("C-0002", ClaimKind.NUMBER, "5", "t")
    with pytest.raises(LibraryError, match="holds claim C-0002"):
        built("{{C-0001}}", {"C-0001": mine})
