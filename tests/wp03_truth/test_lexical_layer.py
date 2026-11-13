"""Lexical layer (L1 to L6): best effort, tuned for precision."""
import pytest

from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.truth import FindingKind, Severity

EN, IT, TA = Language.EN, Language.IT, Language.TA


def verdict(result):
    return "BLOCK" if result.blocks else ("FLAG" if result.flags else "PASS")


# ---------------------------------------------------------------- L1: high-precision blocks

L1 = [
    (EN, "Serving you for a score of years."), (EN, "A couple of decades of service."),
    (EN, "Nearly a decade on the road."), (EN, "A quarter century of trust."), (EN, "Over a century of experience."),
    (EN, "twentyfive years"), (EN, "Fourscore dealers."), (EN, "Quadruple the range."),
    (EN, "Single owner, a pair of trucks."), (EN, "A trio of showrooms."), (EN, "Trusted since the nineties."),
    (EN, "zillions of happy clients"), (EN, "Unbeaten prices"), (EN, "Nobody beats our prices"),
    (EN, "Award-winning dealer"), (EN, "we are the bestest"), (EN, "Unmatchable value"), (EN, "Second to none"),
    (EN, "A bicentennial sale"), (EN, "Triennial warranty"), (EN, "Most popular truck."),
    (IT, "Un ventennio di esperienza."), (IT, "Un decennio al vostro fianco."), (IT, "Da oltre un secolo."),
    (IT, "Un paio di sedi."), (IT, "Una coppia di camion."), (IT, "Una trentina di dipendenti."),
    (IT, "Una quindicina di sedi."), (IT, "Una ventina di anni."), (IT, "Quadruplo della concorrenza."),
    (IT, "Quintuplicato il fatturato."), (IT, "Raddoppiato le vendite."), (IT, "Triplicato le vendite."),
    (IT, "Ventenne esperienza."), (IT, "Trentennale esperienza."), (IT, "Cinquantennale."),
    (IT, "Esperienza decennale."), (IT, "Centenaria."), (IT, "Biennale."), (IT, "Ventesimo anno."),
    (IT, "Trentesimo anniversario."), (IT, "Centesimo cliente."), (IT, "Millesimo cliente."),
    (IT, "Undicesimo anno."), (IT, "Ventunesimo anno."), (IT, "Davvero eccezionale, senza pari."),
    (IT, "Senza rivali."), (IT, "Imbattuto."), (IT, "Miglior prezzo."), (IT, "Peggior nemico dei costi."),
    (IT, "Più venduto."), (IT, "Trentun clienti."), (IT, "Ottantotto clienti."),
    (TA, "தொண்ணூறு ஆண்டுகள்."), (TA, "நாலு கடைகள்."), (TA, "அஞ்சு கடைகள்."), (TA, "பன்னிரண்டு கடைகள்."),
    (TA, "இருமடங்கு"), (TA, "மும்மடங்கு"), (TA, "சதவீதம்"), (TA, "சதவிகிதம்"), (TA, "இரண்டாவது ஆண்டு."),
    (TA, "மூன்றாம் ஆண்டு."),
]


@pytest.mark.parametrize(("language", "text"), L1)
def test_l1_high_precision_patterns_block(run, language, text):
    assert verdict(run(text, language)) == "BLOCK", (text, run(text, language).findings)


# ---------------------------------------------------------------- L2: ambiguous words in context

L2_BLOCK = [
    (EN, "Showrooms: we run one."), (EN, "Our showroom count is one."), (EN, "Customers served: one."),
    (EN, "One showroom."), (EN, "We opened one new showroom this spring."), (EN, "One happy customer after another."),
    (EN, "No. one dealer"), (EN, "Day one."), (EN, "one\nshowroom"), (EN, "Our first-ever showroom."),
    (EN, "#first"), (EN, "Half price today."),
    (IT, "Anni di esperienza: sei."), (IT, "Ne abbiamo aperte sei."), (IT, "Sei nuove sedi."), (IT, "Sei anni."),
    (IT, "Una sola sede in tutta Emilia."), (IT, "Clienti soddisfatti: uno su uno."), (IT, "Il n. uno."),
    (IT, "Prima concessionaria."), (IT, "Il secondo concessionario d'Italia."), (IT, "Un anno di garanzia."),
    (TA, "இரு கடைகள்."), (TA, "அரை விலை"), (TA, "கால் விலை"), (TA, "ஒரு லட்சம்."),
]


@pytest.mark.parametrize(("language", "text"), L2_BLOCK)
def test_l2_ambiguous_word_counting_something_blocks(run, language, text):
    assert verdict(run(text, language)) == "BLOCK", (text, run(text, language).findings)


L2_NOTHING = [
    (IT, "Prenota prima di sabato."), (IT, "Prima di tutto, la sicurezza."), (IT, "Secondo noi è il momento giusto."),
    (IT, "Sei pronto?"), (IT, "Ti aspettiamo, sei dei nostri?"), (IT, "Una nuova esperienza di guida."),
    (IT, "Uno sguardo al futuro."), (IT, "Una nuova sede ti aspetta a Bologna."), (IT, "Uno showroom in centro."),
    (IT, "Una grande famiglia di clienti."), (IT, "Secondo i clienti, funziona."),
    (EN, "No one left behind."), (EN, "One-stop shop."), (EN, "We are one of a kind."), (EN, "Wait a second."),
    (EN, "Second-hand vans."), (EN, "Half-day test drive."), (EN, "Double cab."), (EN, "Crew cab, double rear wheels."),
    (EN, "First, book a test drive."), (EN, "At first glance."),
    (TA, "ஒரு கடை."),
]


@pytest.mark.parametrize(("language", "text"), L2_NOTHING)
def test_l2_articles_pronouns_and_prepositions_produce_nothing(run, language, text):
    result = run(text, language)
    assert verdict(result) == "PASS", (text, result.findings)


L2_FLAG = [(EN, "Only on weekends."), (EN, "The top dealer in Chennai."), (EN, "Our latest model."),
           (IT, "Ottimo per la città."), (IT, "Il massimo del comfort."), (IT, "Garantito dal costruttore."),
           (EN, "Lower your costs."), (EN, "Higher roof version."), (IT, "Più veloce.")]


@pytest.mark.parametrize(("language", "text"), L2_FLAG)
def test_l2_the_few_flag_words_flag(run, language, text):
    result = run(text, language)
    assert verdict(result) == "FLAG", (text, result.findings)


def test_l2_volta_is_not_a_quantity_noun(run):
    assert run("Una volta provato, non torni indietro.", IT).ok
    assert verdict(run("La prima volta.", IT)) == "PASS"


# ---------------------------------------------------------------- L3: universal claims

@pytest.mark.parametrize(("language", "text"), [
    (EN, "It never breaks down."), (EN, "Always the right price."), (IT, "Nessuno vende di più."),
    (IT, "Prezzi sempre chiari."), (EN, "Every delivery on time."),
])
def test_l3_universal_with_a_performance_verb_or_product_noun_flags(run, language, text):
    result = run(text, language)
    assert FindingKind.UNIVERSAL_CLAIM in {f.kind for f in result.findings if f.severity is Severity.FLAG}, text


@pytest.mark.parametrize(("language", "text"), [
    (EN, "Everyone welcome."), (EN, "All-wheel drive."), (EN, "Every year since forever."),
    (IT, "Tutte le versioni disponibili."), (IT, "Dove il servizio è di casa, sempre."), (IT, "Quasi tutti i clienti."),
])
def test_l3_universal_without_one_produces_nothing(run, language, text):
    assert verdict(run(text, language)) == "PASS", text


# ---------------------------------------------------------------- L4: fixed phrases and Roman numerals

@pytest.mark.parametrize(("language", "text"), [
    (EN, "On top of that, free coffee."), (EN, "Top up your fleet."), (EN, "A top-up for your fleet."),
    (EN, "Come in at least once."), (EN, "Last but not least."), (EN, "First of all, thank you."),
    (EN, "Rather than wait, book now."), (EN, "Most of our vans."), (EN, "The XL van."), (EN, "Size XL."),
    (EN, "Model DC."), (EN, "CV joints."), (EN, "Iveco Daily Euro VI."), (IT, "Il furgone XL."), (IT, "Versione LX."),
    (IT, "Modello CC."), (EN, "Unique design."), (EN, "Leading the way."), (EN, "Record your visit."),
    (EN, "Proven engine."),
])
def test_l4_fixed_phrases_and_two_letter_codes_produce_nothing(run, language, text):
    result = run(text, language)
    assert verdict(result) == "PASS", (text, result.findings)


@pytest.mark.parametrize(("language", "text"), [
    (EN, "Year II of our journey."), (IT, "Il XX secolo."), (EN, "Serving you for XV years."), (EN, "XII"),
    (EN, "Over xii years."), (EN, "MCMXC"),
])
def test_l4_roman_numerals_next_to_a_year_or_long_enough_block(run, language, text):
    assert verdict(run(text, language)) == "BLOCK", text


# ---------------------------------------------------------------- L5: per-tenant allowed terms

@pytest.mark.parametrize(("language", "text", "allowed"), [
    (IT, "Fiat Seicento in pronta consegna.", {"Seicento"}),
    (IT, "Vieni nella sede di Sesto San Giovanni.", {"Sesto San Giovanni"}),
    (IT, "Concessionaria di Settimo Torinese.", {"Settimo Torinese"}),
    (IT, "Via Venti Settembre.", {"Via Venti Settembre"}),
    (IT, "Fiat Cinquecento.", {"cinquecento"}),
    (EN, "Visit us in Trento.", {"Trento"}),
])
def test_l5_allowed_terms_never_produce_a_finding(run, language, text, allowed):
    assert not run(text, language).ok or text.endswith("Trento."), text
    assert verdict(run(text, language, allowed_terms=frozenset(allowed))) == "PASS", text


def test_l5_allowed_terms_do_not_reach_beyond_the_term(run):
    allowed = frozenset({"Seicento"})
    assert not run("Fiat Seicento, il migliore.", IT, allowed_terms=allowed).ok
    assert not run("Seicento clienti e 7 sedi.", IT, allowed_terms=allowed).ok  # digit is structural


# ---------------------------------------------------------------- L6: names and split words

@pytest.fixture
def names(make):
    return {"C-0003": make("C-0003", ClaimKind.NAME, "Rossi Trasporti")}


@pytest.mark.parametrize("text", ["Rossi", "Ask Rossi about it.", "Rossi's Trasporti", "Rossis Trasporti",
                                  "Rossi Tras-\nporti", "Ro-ssi Tra-sporti", "R o s s i Trasporti",
                                  "Rossi_Trasporti", "Trasporti Rossi"])
def test_l6_names_match_their_distinctive_tokens_and_split_forms(run, names, text):
    result = run(text, claims=names)
    assert FindingKind.NAMED_CUSTOMER in {f.kind for f in result.blocks}, (text, result.findings)


def test_l6_common_words_of_a_name_alone_pass(run, names):
    assert run("Trasporti e logistica in Emilia.", IT, claims=names).ok
    assert run("Ask Rossini.", claims=names).ok


@pytest.mark.parametrize(("language", "text"), [
    (EN, "b/e/s/t"), (EN, "b|e|s|t"), (EN, "b+e+s+t"), (EN, "b'e's't"), (EN, "be**st"), (EN, "be_st"),
    (EN, "bes t"), (EN, "b est"), (EN, "te-n years"), (IT, "m-igliore"), (EN, "b.e.s.t"), (EN, "t e n years"),
])
def test_l6_words_split_by_separators_are_rejoined(run, language, text):
    assert verdict(run(text, language)) == "BLOCK", text


def test_l6_ordinary_abbreviations_are_not_rejoined(run):
    assert run("Rossi Auto S.r.l. e Bianchi S.p.A.", IT).ok
    assert run("VW ID.Buzz LWB, e-mail and km/h.").ok
