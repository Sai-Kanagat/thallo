import pytest

from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.truth import CompileFailed, Finding, FindingKind, build, compile_text
from tests.wp03_truth.conftest import DAY


def kinds(result):
    return [f.kind for f in result.findings]


def run(text, clock, language=Language.EN, claims=None, **kw):
    return compile_text(text, language=language, claims=claims or {}, clock=clock, **kw)


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
def test_any_digit_outside_a_slot_fails(clock, digit):
    result = run(f"We sold {digit} units.", clock)
    assert not result.ok
    assert FindingKind.DIGIT in kinds(result)


def test_decimal_outside_a_slot_is_one_finding_with_its_span(clock):
    result = run("Payload 3.5 tonnes. Call us.", clock)
    digits = [f for f in result.findings if f.kind is FindingKind.DIGIT]
    assert [(f.match, f.sentence) for f in digits] == [("3", 0), ("5", 0)]
    assert result.text[digits[0].start:digits[0].end] == "3"


def test_currency_and_percent_signs_fail(clock):
    assert FindingKind.SYMBOL in kinds(run("Save big in € today.", clock))
    assert FindingKind.SYMBOL in kinds(run("Zero interest %.", clock))


# ---------------------------------------------------------------- superlatives and comparatives

@pytest.mark.parametrize(("language", "text", "kind"), [
    (Language.EN, "The best dealer in town.", FindingKind.SUPERLATIVE),
    (Language.EN, "A world-class service.", FindingKind.SUPERLATIVE),
    (Language.EN, "Cheaper than the rest.", FindingKind.COMPARATIVE),
    (Language.EN, "Twelve showrooms.", FindingKind.NUMBER_WORD),
    (Language.IT, "Il servizio migliore di Bologna.", FindingKind.SUPERLATIVE),
    (Language.IT, "Prezzi convenientissimi.", FindingKind.SUPERLATIVE),
    (Language.IT, "Il più veloce in città.", FindingKind.SUPERLATIVE),
    (Language.IT, "Più veloce della concorrenza.", FindingKind.COMPARATIVE),
    (Language.IT, "Meglio rispetto agli altri.", FindingKind.COMPARATIVE),
    (Language.IT, "Garantito per sempre.", FindingKind.SUPERLATIVE),
    (Language.IT, "Tre anni di garanzia.", FindingKind.NUMBER_WORD),
    (Language.IT, "The best in Italia.", FindingKind.SUPERLATIVE),  # English inside Italian copy
    (Language.TA, "சென்னையில் சிறந்த டீலர்.", FindingKind.SUPERLATIVE),
    (Language.TA, "மிகக் குறைந்த விலை.", FindingKind.SUPERLATIVE),
    (Language.TA, "சென்னையின் முன்னணி டீலர்.", FindingKind.SUPERLATIVE),
    (Language.TA, "மற்றவர்களை விட வேகமாக.", FindingKind.COMPARATIVE),
    (Language.TA, "இதை விடக் குறைவு.", FindingKind.COMPARATIVE),
    (Language.TA, "ஐந்து ஆண்டு உத்தரவாதம்.", FindingKind.NUMBER_WORD),
    (Language.TA, "Best offer இன்று.", FindingKind.SUPERLATIVE),  # English inside Tamil copy
])
def test_per_language_lists(clock, language, text, kind):
    result = run(text, clock, language)
    assert kind in kinds(result), result.findings


@pytest.mark.parametrize(("language", "text"), [
    (Language.EN, "Learn more about our three-wheelers. Book a test ride today."),
    (Language.EN, "We would love to hear from you, so talk to someone on our team."),
    (Language.IT, "Scopri di più sul nostro servizio. Nel rispetto dell'ambiente."),
    (Language.IT, "Prima di partire, chiamaci. Sei pronto?"),
    (Language.TA, "அதிகாரப்பூர்வ டீலர். விடுமுறை நாளிலும் திறந்திருக்கும்."),
])
def test_ordinary_copy_passes(clock, language, text):
    result = run(text, clock, language)
    assert result.ok, result.findings


def test_italian_superlatives_do_not_leak_into_english_copy(clock):
    assert run("Our team is leader-free.", clock).findings == ()


# ---------------------------------------------------------------- names, hidden characters, slots

def test_named_customer_outside_a_slot_fails_any_claim_state(clock, make):
    name = make("C-0001", ClaimKind.NAME, "Rossi Trasporti", verified=False, languages={"ta": "ரோஸி"})
    claims = {name.code: name}
    assert FindingKind.NAMED_CUSTOMER in kinds(run("Ask rossi  trasporti how it went.", clock, claims=claims))
    assert FindingKind.NAMED_CUSTOMER in kinds(run("ரோஸி சொன்னது.", clock, Language.TA, claims=claims))
    assert FindingKind.NAMED_CUSTOMER in kinds(run("Ask Bianchi Srl.", clock, protected_names=["Bianchi Srl"]))
    assert run("Ask Rossini.", clock, claims=claims).ok


def test_hidden_character_cannot_split_a_word(clock):
    result = run("The be​st dealer.", clock)
    assert FindingKind.HIDDEN_CHARACTER in kinds(result)


@pytest.mark.parametrize("text", ["Price {{C-12}}.", "Price {C-0001}.", "Price [[C-0001]].", "Price {{ C-0001 }}."])
def test_malformed_slots_fail(clock, text):
    assert FindingKind.MALFORMED_SLOT in kinds(run(text, clock))


def test_slots_need_live_verified_allowed_claims(clock, make):
    good = make("C-0001", ClaimKind.NUMBER, "3.5", "t")
    pending = make("C-0002", ClaimKind.NUMBER, "40", verified=False)
    short = make("C-0003", ClaimKind.PRICE, "9.99", "EUR", verify_expires_at=clock.now() + DAY)
    claims = {c.code: c for c in (good, pending, short)}

    ok = run("It carries {{C-0001}}. Simply honest.", clock, claims=claims)
    assert ok.ok and ok.codes == ("C-0001",) and ok.claim_ids == (good.id,)

    assert kinds(run("{{C-0009}}", clock, claims=claims)) == [FindingKind.UNKNOWN_CLAIM]
    assert kinds(run("{{C-0002}}", clock, claims=claims)) == [FindingKind.UNUSABLE_CLAIM]
    assert kinds(run("{{C-0001}}", clock, claims=claims, allowed_claim_ids=[short.id])) == [
        FindingKind.CLAIM_NOT_ALLOWED]
    assert run("{{C-0003}}", clock, claims=claims).ok
    clock.advance(DAY)
    assert kinds(run("{{C-0003}}", clock, claims=claims)) == [FindingKind.UNUSABLE_CLAIM]


def test_digits_inside_a_slot_value_are_fine_but_not_beside_it(clock, make):
    claims = {"C-0001": make("C-0001", ClaimKind.NUMBER, "3.5", "t")}
    assert run("Carries {{C-0001}}.", clock, claims=claims).ok
    assert FindingKind.DIGIT in kinds(run("Carries {{C-0001}}0.", clock, claims=claims))


def test_detector_hook_sees_masked_text_and_can_block(clock, make):
    claims = {"C-0001": make("C-0001", ClaimKind.NAME, "Acme")}
    seen = []

    class Flagger:
        def detect(self, masked_text, sentences, language):
            seen.append(masked_text)
            i = masked_text.find("legendary")
            return [Finding(FindingKind.CLASSIFIER, i, i + 9, "legendary", "model: superlative")] if i >= 0 else []

    result = run("{{C-0001}} is legendary.", clock, claims=claims, detectors=[Flagger()])
    assert kinds(result) == [FindingKind.CLASSIFIER]
    assert "{{" not in seen[0]


def test_build_compiles_then_fills_or_raises(clock, make):
    claims = {"C-0001": make("C-0001", ClaimKind.NUMBER, "3.5", "t")}
    built = build("Porta {{C-0001}}.", language=Language.IT, claims=claims, clock=clock,
                  default_language=Language.EN)
    assert built.filled.text == "Porta 3,5 tonnellate."
    with pytest.raises(CompileFailed) as err:
        build("Porta {{C-0001}}, il migliore.", language=Language.IT, claims=claims, clock=clock,
              default_language=Language.EN)
    assert err.value.result.findings[0].kind is FindingKind.SUPERLATIVE
