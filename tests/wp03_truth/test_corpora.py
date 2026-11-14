"""O6: precision and recall regression corpora.

PRECISION: ordinary dealership and B2B software copy with no numbers and no superlatives, including the
review's false blocks. Zero blocks and at most three flags in total, or approvers learn to tap through.
RECALL: every bypass string from the review's blocking sections (number words, superlatives, slots,
lookalikes, names). Every one must block.
"""
import pytest

from thallo.contracts.enums import ClaimKind, Language

EN, IT, TA = Language.EN, Language.IT, Language.TA

PRECISION_IT = [
    "Una nuova sede ti aspetta a Bologna.",
    "Sei alla ricerca di un furgone affidabile?",
    "Prima di decidere, vieni a provarne uno.",
    "Secondo i nostri clienti, è una scelta che fa la differenza.",
    "Tutte le versioni sono disponibili, e il nostro team è sempre pronto ad aiutarti.",
    "Vieni a trovarci a Trento.",
    "Prenota prima di sabato.",
    "Prima di tutto, la sicurezza.",
    "Secondo noi è il momento giusto.",
    "Sei pronto?",
    "Ti aspettiamo, sei dei nostri?",
    "Una nuova esperienza di guida.",
    "Uno sguardo al futuro.",
    "Dove il servizio è di casa, sempre.",
    "La prima volta non si scorda.",
    "Una volta provato, non torni indietro.",
    "Siamo a Trieste e a Treviso.",
    "Nuova sede, stessa passione.",
    "Ti presentiamo il nuovo Ducato.",
    "Iveco Daily Euro VI disponibile in pronta consegna.",
    "Il furgone XL è pronto per il ritiro.",
    "Ottimizzare i costi della flotta è semplice.",
    "Il nostro team commerciale ti segue passo dopo passo.",
    "Richiedi un preventivo senza impegno.",
    "Scopri la gamma di veicoli commerciali.",
    "Il centro assistenza è aperto anche il sabato.",
    "Offriamo finanziamenti su misura per la tua azienda.",
    "Parla con un nostro consulente.",
    "Un consulente ti richiama in giornata.",
    "Ricevi un avviso quando un veicolo richiede manutenzione.",
    "La piattaforma si integra con il tuo gestionale.",
    "Automatizza le attività ripetitive del tuo team.",
    "Prova la demo gratuita.",
    "I tuoi dati restano al sicuro su server europei.",
    "Il supporto risponde in italiano.",
    "Configura i flussi di lavoro senza scrivere codice.",
    "Esporta i report in un clic.",
    "Collega la tua email e il calendario.",
    "Monitora la flotta in tempo reale.",
    "Ti aspettiamo in concessionaria per un caffè.",
    "Rossi Auto S.r.l. e Bianchi S.p.A. sono partner storici.",
    "La gamma completa, dal più piccolo al più grande.",
]

PRECISION_EN = [
    "First, book a test drive.",
    "At first glance, it looks like any van.",
    "Come in at least once.",
    "Last but not least, free coffee.",
    "On top of that, free coffee.",
    "Top up your fleet with a new van.",
    "Rather than wait, book now.",
    "Second-hand vans with full history.",
    "Wait a second, there is more.",
    "Half-day test drive on request.",
    "Double cab available to order.",
    "Crew cab, double rear wheels.",
    "Record your visit in the app.",
    "Proven engine, simple service.",
    "Unique design inside and out.",
    "Leading the way on electric vans.",
    "Most of our vans are in stock.",
    "Everyone welcome.",
    "All-wheel drive on request.",
    "No one left behind.",
    "One-stop shop for your fleet.",
    "The XL van fits a pallet.",
    "Visit us in Trento.",
    "Iveco Daily Euro VI in stock.",
    "Mild hybrid, mid-size van.",
    "Mix and match the options.",
    "We opened a new showroom this spring.",
    "Our platform connects to your CRM.",
    "Set up workflows without writing code.",
    "Your data stays in the EU.",
    "Talk to our sales team.",
    "Try the demo today.",
    "Invite your team and start working together.",
    "Reports update in real time.",
    "Connect the tools you already use.",
    "Get an alert when a vehicle needs service.",
    "Export reports in one click.",
    "We are one of a kind.",
    "Book online or call the showroom.",
    "Ask about fleet finance for your business.",
    "VW ID.Buzz LWB, e-mail and km/h.",
]


def test_precision_corpus_has_no_blocks_and_almost_no_flags(run):
    assert len(PRECISION_IT) >= 40 and len(PRECISION_EN) >= 40
    blocks, flags = [], []
    for language, corpus in ((IT, PRECISION_IT), (EN, PRECISION_EN)):
        for text in corpus:
            result = run(text, language)
            blocks += [(text, f.kind.value, f.match) for f in result.blocks]
            flags += [(text, f.kind.value, f.match) for f in result.flags]
    assert blocks == []
    assert len(flags) <= 3, flags


RECALL = [
    # 1 number words
    (EN, "Serving you for a score of years."), (EN, "A couple of decades of service."), (EN, "Nearly a decade."),
    (EN, "Over a century of experience."), (EN, "twentyfive years"), (EN, "Fourscore dealers."),
    (EN, "Quadruple the range."), (EN, "A pair of trucks."), (EN, "A trio of showrooms."), (EN, "Since the nineties."),
    (EN, "zillions of happy clients"), (EN, "Biennial service"), (EN, "Showrooms: we run one."),
    (EN, "Our showroom count is one."), (EN, "Customers served: one."), (EN, "One happy customer after another."),
    (IT, "Un ventennio di esperienza."), (IT, "Un decennio al vostro fianco."), (IT, "Da oltre un secolo."),
    (IT, "Un paio di sedi."), (IT, "Una coppia di camion."), (IT, "Una trentina di dipendenti."),
    (IT, "Una ventina di anni."), (IT, "Quadruplo della concorrenza."), (IT, "Quintuplicato il fatturato."),
    (IT, "Raddoppiato le vendite."), (IT, "Ventenne esperienza."), (IT, "Trentennale esperienza."),
    (IT, "Centenaria."), (IT, "Ventesimo anno."), (IT, "Millesimo cliente."), (IT, "Ventunesimo anno."),
    (IT, "Anni di esperienza: sei."), (IT, "Ne abbiamo aperte sei."), (IT, "Sei nuove sedi."),
    (IT, "Una sola sede in tutta Emilia."), (IT, "Clienti soddisfatti: uno su uno."),
    (TA, "தொண்ணூறு ஆண்டுகள்."), (TA, "நாலு கடைகள்."), (TA, "அஞ்சு கடைகள்."), (TA, "பன்னிரண்டு கடைகள்."),
    (TA, "இருமடங்கு"), (TA, "சதவீதம்"), (TA, "இரு கடைகள்."), (TA, "அரை விலை"), (TA, "இரண்டாவது ஆண்டு."),
    # 2 superlatives and comparisons
    (EN, "Unbeaten prices"), (EN, "Nobody beats our prices"), (EN, "Award-winning dealer"),
    (EN, "we are the bestest"), (EN, "Unmatchable value"), (EN, "Second to none"), (EN, "Numero uno in Chennai"),
    (EN, "No. one dealer"), (EN, "Number-one dealer"), (EN, "#first"), (EN, "Our first-ever showroom."),
    (EN, "Most popular truck."), (EN, "No one sells more trucks"),
    (IT, "Davvero eccezionale, senza pari."), (IT, "Senza rivali."), (IT, "Imbattuto."), (IT, "Miglior prezzo."),
    (IT, "Più venduto."), (IT, "Il n. uno."), (IT, "Prima concessionaria."),
    # 3 slots
    *[(EN, s) for s in (
        "{{C-0001}} {{C-0004}} trucks.", "{{C-0001}} {{C-0004}} trucks.", "{{C-0001}} {{C-0004}} trucks.",
        "{{C-0001}}\u2013{{C-0002}} days.", "{{C-0001}}\u2014{{C-0002}} days.", "{{C-0001}}·{{C-0002}} rating.",
        "{{C-0001}}…{{C-0002}}", "{{C-0001}}․{{C-0002}}", "{{C-0001}}。{{C-0002}}",
        "{{C-0001}}．{{C-0002}}", "{{C-0001}}٫{{C-0002}}", "{{C-0001}}; {{C-0002}}",
        "{{C-0001}}-fold growth.", "{{C-0001}} times the output.", "{{C-0001}} grand.", "{{C-0001}} b in sales.",
        "{{C-0001}} bln in sales.", "{{C-0001}} lac customers.", "{{C-0001}}-k customers", "{{C-0001}}-plus years.",
        "{{C-0001}} st place", "x {{C-0001}} the output", "#{{C-0001}} in Emilia.", "No. {{C-0001}} dealer.",
        "-{{C-0001}} on every truck.", "({{C-0001}}){{C-0002}}", "{{C-0001}}){{C-0002}}", "{{C-0001}}\"{{C-0002}}",
        "[{{C-0001}}]{{C-0002}}", "{{C-0001}}\n{{C-0002}}")],
    (IT, "{{C-0001}} volte il fatturato."),
    # 4 lookalikes, enclosed digits and split words
    (EN, "\U0001f171\U0001f174\U0001f182\U0001f183"), (EN, "\U0001f51f years"), (EN, "\U0001f947 dealer"),
    (EN, "\U0001f4af satisfied"), (EN, "b/e/s/t"), (EN, "b|e|s|t"), (EN, "b+e+s+t"), (EN, "b'e's't"),
    (EN, "bes t"), (EN, "b est"), (EN, "be**st"), (EN, "be_st"), (EN, "te-n years"), (IT, "m-igliore"),
    # 5 names
    (EN, "Rossi"), (EN, "Rossi Tras-\nporti"), (EN, "Rossi's Trasporti"), (EN, "Rossis Trasporti"),
    (EN, "Ro-ssi Tra-sporti"), (EN, "Trasporti Rossi"),
]


@pytest.fixture
def lib(make):
    return {c.code: c for c in (make("C-0001", ClaimKind.NUMBER, "10", "year"),
                                make("C-0002", ClaimKind.NUMBER, "5", "day"),
                                make("C-0003", ClaimKind.NAME, "Rossi Trasporti"),
                                make("C-0004", ClaimKind.NUMBER, "500", "km"))}


def test_recall_corpus_blocks_every_bypass(run, lib):
    misses = [(lang.value, text) for lang, text in RECALL if run(text, lang, claims=lib).ok]
    assert misses == []
