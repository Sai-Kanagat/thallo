"""Per-language word lists, number parsers and character rules for the compile step.

Two kinds of rule live here, used by two layers (see `truth/README.md`):

STRUCTURAL helpers (complete): `is_digit_char`, `is_money_or_percent`, `is_hidden_char`,
`is_enclosed_digit` and `foreign_letter`. Digits are universal and need no list: any character
whose Unicode category is Nd, No or Nl counts (ASCII, Tamil, Devanagari, Arabic-Indic, fullwidth,
superscripts, fractions, Roman numeral characters).

LEXICAL lists (best effort, tuned for precision): words that BLOCK on their own because they are
almost never anything but a number, superlative or comparison (`BLOCK_WORDS`, the number parsers);
ambiguous words that only block when they count something (`COUNT_WORDS`, decided in context by
`lexical.py`); the few words that only FLAG (`FLAG_WORDS`, `FLAG_COMPARATIVES`); universal claims;
and fixed phrases that never produce a finding (`EXCEPTION_PHRASES`). English is always checked
too, since English words turn up in Italian and Tamil copy.

Lookalikes. Every list runs on a folded view of the text: each character goes through NFKC
(fullwidth, mathematical letters, ligatures, long s), casefold, and loses Latin combining marks.
Letters from scripts a language does not use (Cyrillic, Greek, small capitals, IPA) are refused
outright by `foreign_letter`, so a Cyrillic letter inside "best" fails the build.

The Tamil lists are a first draft and need a native reader.
"""
from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from functools import cache

from thallo.contracts.enums import Language


class Category(StrEnum):
    SUPERLATIVE = "superlative"
    COMPARATIVE = "comparative"
    NUMBER_WORD = "number_word"


# ---------------------------------------------------------------- character classes

def _mark_class() -> str:
    """Every combining mark in the Basic Multilingual Plane, as a regex character class body."""
    ranges: list[tuple[int, int]] = []
    for cp in range(0x10000):
        if unicodedata.category(chr(cp)) in ("Mn", "Mc", "Me"):
            if ranges and ranges[-1][1] == cp - 1:
                ranges[-1] = (ranges[-1][0], cp)
            else:
                ranges.append((cp, cp))
    return "".join(f"\\u{a:04x}" if a == b else f"\\u{a:04x}-\\u{b:04x}" for a, b in ranges)


MARKS = _mark_class()
WORD_CHAR = rf"[\w{MARKS}]"
LEFT = rf"(?<!{WORD_CHAR})"
RIGHT = rf"(?!{WORD_CHAR})"
_LETTERS = rf"(?:[^\W\d_]|[{MARKS}])"
WORD = re.compile(rf"(?:[^\W\d_]|[{MARKS}])+")  # a word, Tamil vowel signs included

_EXTRA_HIDDEN = frozenset({0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}
                          | set(range(0xFE00, 0xFE10)) | set(range(0xE0100, 0xE01F0)))

# S3: keycap and enclosed digits that are not Unicode digits (U+1F51F keycap ten is a symbol).
# Regional indicators (U+1F1E6 to U+1F1FF, country flags) carry no number and are left out.
_ENCLOSED = ((0x1F100, 0x1F1E5), (0x1F51F, 0x1F51F), (0x1F4AF, 0x1F4AF), (0x1F947, 0x1F949),
             (0x2460, 0x24FF), (0x2776, 0x2793), (0x20E3, 0x20E3))
# Rating stars in a row ("five stars" written as symbols).
STARS = frozenset(chr(c) for c in (0x2605, 0x2606, 0x2729, 0x272A, 0x272B, 0x272C, 0x272D, 0x272E, 0x272F,
                                   0x2730, 0x2B50, 0x1F31F))


@cache
def is_digit_char(ch: str) -> bool:
    return unicodedata.category(ch) in ("Nd", "No", "Nl")


@cache
def is_hidden_char(ch: str) -> bool:
    """Invisible characters that can split or disguise a word: format characters (zero-width
    space, joiners, BOM, soft hyphen), the combining grapheme joiner, variation selectors, Hangul
    fillers and the blank Braille pattern."""
    return unicodedata.category(ch) == "Cf" or ord(ch) in _EXTRA_HIDDEN


@cache
def is_money_or_percent(ch: str) -> bool:
    return unicodedata.category(ch) == "Sc" or ch in "%‰‱٪﹪％"


@cache
def is_enclosed_digit(ch: str) -> bool:
    """S3: keycap, enclosed and circled number or letter symbols, and the keycap combiner."""
    cp = ord(ch)
    return any(a <= cp <= b for a, b in _ENCLOSED)


_LATIN_MARKS = re.compile("[̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-︯]")
_LATIN_LETTERS = ((0x41, 0x5A), (0x61, 0x7A), (0xAA, 0xAA), (0xBA, 0xBA), (0xC0, 0x24F))
_TAMIL = (0x0B80, 0x0BFF)


@cache
def fold_char(ch: str) -> str:
    """NFKC, casefold, then drop Latin combining marks. May return "" or several characters."""
    out = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", ch).casefold())
    return unicodedata.normalize("NFC", _LATIN_MARKS.sub("", out))


def fold(text: str) -> tuple[str, list[int]]:
    """The folded text and, for each folded character, the index of the original character."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        f = fold_char(ch)
        if len(f) == 1:
            chars.append(f)
            origin.append(i)
        else:
            for c in f:
                chars.append(c)
                origin.append(i)
    return "".join(chars), origin


def fold_str(text: str) -> str:
    return fold(text)[0]


@cache
def foreign_letter(ch: str, language: Language) -> bool:
    """True for a letter or mark from a script the language may not use outside a slot.
    en, it: Latin. ta: Tamil plus Latin. Punctuation, spaces and symbols are Common and pass."""
    for c in unicodedata.normalize("NFKC", ch):
        cat = unicodedata.category(c)
        cp = ord(c)
        if cat[0] == "L":
            if any(a <= cp <= b for a, b in _LATIN_LETTERS):
                continue
            if language is Language.TA and _TAMIL[0] <= cp <= _TAMIL[1]:
                continue
            return True
        if cat[0] == "M":
            if _LATIN_MARKS.match(c):
                continue
            if language is Language.TA and _TAMIL[0] <= cp <= _TAMIL[1]:
                continue
            return True
    return False


def _f(words: str) -> frozenset[str]:
    return frozenset(fold_str(w) for w in words.split())


# ---------------------------------------------------------------- BLOCK: single words

_EN_TENS = "twenty thirty forty fifty sixty seventy eighty ninety"
_EN_SMALL = ("two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
             "seventeen eighteen nineteen")

BLOCK_WORDS: dict[Language, dict[Category, frozenset[str]]] = {
    Language.EN: {
        Category.SUPERLATIVE: _f(
            "best worst biggest largest smallest fastest quickest cheapest lowest highest greatest strongest "
            "safest finest oldest unbeatable unbeaten unmatched unmatchable unrivalled unrivaled unparalleled "
            "unsurpassed bestseller bestsellers bestselling most least"),
        Category.COMPARATIVE: _f("than versus vs outperform outperforms outperformed outperforming outsell "
                                 "outsells outsold"),
        Category.NUMBER_WORD: _f(
            f"zero {_EN_SMALL} {_EN_TENS} hundred hundreds thousand thousands million millions billion billions "
            "trillion trillions zillion zillions gazillion gazillions dozen dozens twice thrice triple quadruple "
            "quintuple lakh lakhs crore crores percent decade decades century centuries trio third fourth fifth "
            "sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth "
            "seventeenth eighteenth nineteenth twentieth thirtieth fortieth fiftieth sixtieth seventieth "
            "eightieth ninetieth hundredth thousandth millionth mln mld mrd bn mn biennial triennial "
            "quadrennial centennial bicentennial tricentennial twenties thirties forties fifties sixties "
            "seventies eighties nineties"),
    },
    Language.IT: {
        Category.SUPERLATIVE: _f(
            "migliore migliori miglior peggiore peggiori peggior pessimo pessima pessimi pessime imbattibile "
            "imbattibili ineguagliabile ineguagliabili insuperabile insuperabili ineguagliato ineguagliata "
            "ineguagliati ineguagliate leader"),
        Category.COMPARATIVE: frozenset(),
        Category.NUMBER_WORD: _f(
            "zero milione milioni miliardo miliardi percento paio paia secolo secoli doppio doppia doppi "
            "doppie triplo tripla tripli triple quadruplo quadrupla quintuplo"),
    },
    Language.TA: {
        Category.SUPERLATIVE: _f("ஒரே மட்டுமே மிகக் மிகச் மிகப் மிகத்"),
        Category.COMPARATIVE: _f("விட"),
        Category.NUMBER_WORD: _f("பாதி இரட்டை"),
    },
}

# Word starts that block (folded).
BLOCK_PREFIXES: dict[Language, dict[Category, tuple[str, ...]]] = {
    Language.EN: {Category.SUPERLATIVE: ("best", "unbeat", "unmatch", "unrival"),
                  Category.COMPARATIVE: ("outperform", "outsell")},
    Language.IT: {Category.SUPERLATIVE: ("imbattut",),
                  Category.NUMBER_WORD: ("migliai", "centinai", "decin", "dozzin", "raddoppi", "triplic",
                                         "quadruplic", "quintuplic", "dimezz", "quintupl", "quadrupl")},
    Language.TA: {Category.SUPERLATIVE: ("சிறந்த", "முதலிடம்", "முதன்மை", "முன்னணி", "தனித்துவ", "நிகரற்ற",
                                         "உலகத்தர", "அதிகபட்ச", "குறைந்தபட்ச"),
                  Category.COMPARATIVE: ("விடக்", "விடச்", "விடப்", "விடவும்", "ஒப்பிட")},
}

# Multi-word or shaped patterns that block, as regex on folded text (wrapped in LEFT, RIGHT).
_H = r"[-\s]?"
BLOCK_PATTERNS: dict[Language, dict[Category, tuple[str, ...]]] = {
    Language.EN: {
        Category.SUPERLATIVE: (
            rf"world{_H}class", rf"market{_H}leaders?", rf"industry{_H}leaders?", rf"number{_H}one",
            r"numero\s+uno", rf"award{_H}winning", r"second\s+to\s+none",
            r"(?:nobody|no\s*one|no-one)\s+(?:beats|outsells|sells\s+more|offers\s+more|does\s+it\s+better)",
        ),
        Category.COMPARATIVE: (r"compared\s+(?:to|with)",),
        Category.NUMBER_WORD: (
            r"per\s+cent", r"(?:a\s+)?couple\s+of", r"pairs?\s+of", r"scores?\s+of", r"(?:double|triple)\s+the",
            rf"(?:many|multi){_H}fold", r"half\s+(?:price|off|the\s+price)",
        ),
    },
    Language.IT: {
        Category.SUPERLATIVE: (
            rf"{_LETTERS}+issim[oaie]",
            r"(?:il|la|lo|i|gli|le|l['’])\s*(?:piu|meno)",
            r"il\s+meglio",
            r"senza\s+(?:pari|rivali|eguali|uguali|confronti|paragoni)",
            r"(?:piu|meno)\s+vendut[oaie]",
            r"numero\s+uno",
        ),
        Category.COMPARATIVE: (
            rf"(?:piu|meno)\s+{_LETTERS}+\s+(?:di|del|dello|della|dei|degli|delle|dell['’]|che)",
            r"(?:piu|meno)\s+(?:di|del|dello|della|dei|degli|delle|che)",
            r"rispetto\s+(?:a|al|allo|alla|ai|agli|alle|all['’])",
            r"(?:in\s+)?confronto\s+(?:a|con|al|allo|alla|ai|agli|alle|all['’])",
        ),
        Category.NUMBER_WORD: (
            r"(?:terz|quart|quint|sest|settim|ottav|non|decim)[oa]",
            r"per\s+cento",
            r"coppi[ae]\s+di",
            r"meta\s+prezzo",
        ),
    },
    Language.TA: {Category.SUPERLATIVE: ("நம்பர்\\s+ஒன்",)},
}

# Tamil number roots matched anywhere inside a word (Tamil writes numbers as one word with
# sandhi): பதினைந்து, இருநூறு, எழுபது, ஆயிரக்கணக்கான. பதின (teens) rather than பதி, so பதிவு
# (registration) is not caught.
TA_INFIX = ("பதின", "பத்து", "பது", "நூறு", "நூற்", "ஆயிர", "லட்ச", "கோடி", "ஒன்று", "இரண்டு", "மூன்று",
            "நான்கு", "ஐந்து", "ஆறு", "ஏழு", "எட்டு", "ஒன்பது", "தொண்ணூ", "பன்னிர", "நாலு", "அஞ்சு", "மடங்கு", "சதவ")
# Sandhi stems: a number root that has lost its final u before a suffix (இரண்டாவது, மூன்றாம்).
# They match only when the next character is the long a sign or u sign, so பத்திரிகை (newspaper)
# and ஏழை (poor) do not.
TA_STEMS = ("இரண்ட", "மூன்ற", "நான்க", "ஐந்த", "ஏழ", "எட்ட", "ஒன்பத", "பத்த")
TA_STEM_NEXT = frozenset("ாு")  # long a sign, u sign


# ---------------------------------------------------------------- number parsers (closed compounds)

_EN_ROOTS = tuple(sorted(_f(
    f"one {_EN_SMALL} {_EN_TENS} zero hundred thousand million billion score dozen hundreds thousands millions "
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth twentieth thirtieth "
    "fortieth fiftieth sixtieth seventieth eightieth ninetieth hundredth thousandth"), key=len, reverse=True))
MAX_WORD = 64


def english_number_word(word: str) -> bool:
    """A folded closed compound of two or more English number roots (twentyfive, fourscore,
    onehundred), optionally ending in -fold (tenfold). Linear in the word length."""
    if not word or len(word) > MAX_WORD:
        return False
    if word.endswith("fold") and len(word) > 4:
        stem = word[:-4]
        return stem in _EN_ROOTS or english_number_word(stem)
    # best[i] = the most roots a segmentation of word[i:] can use, or -1
    n = len(word)
    best = [-1] * (n + 1)
    best[n] = 0
    for i in range(n - 1, -1, -1):
        for root in _EN_ROOTS:
            if word.startswith(root, i) and best[i + len(root)] >= 0:
                best[i] = max(best[i], best[i + len(root)] + 1)
    return best[0] >= 2


_IT_UNITS = ("uno", "due", "tre", "quattro", "cinque", "sei", "sette", "otto", "nove")
_IT_TEENS = ("dieci", "undici", "dodici", "tredici", "quattordici", "quindici", "sedici", "diciassette",
             "diciotto", "diciannove")
_IT_TENS = ("venti", "trenta", "quaranta", "cinquanta", "sessanta", "settanta", "ottanta", "novanta")
_IT_BIG = ("cento", "mille", "mila", "milione", "milioni", "miliardo", "miliardi")
_IT_FULL = tuple(sorted({*_IT_UNITS, *_IT_TEENS, *_IT_TENS, *_IT_BIG}, key=len, reverse=True))
# Elided before a vowel: ventuno, trentotto, centottanta.
_IT_ELIDED = tuple(t[:-1] for t in _IT_TENS) + ("cent",)
IT_ARTICLE_NUMBERS = frozenset({"un", "uno", "una"})


def italian_number_word(word: str) -> bool:
    """A folded word built only from Italian number pieces: trentacinque, ventitre, duemilacinquecento,
    ventun, centottanta. An elided ten (vent, trent, cent) must be followed by a piece starting
    with u or o, and "un" only ends a compound (ventun). "sei", "uno" and "un" alone are left to
    the context rules. Linear in the word length; no regex, so no backtracking."""
    if not word or len(word) > MAX_WORD or word in IT_ARTICLE_NUMBERS or word == "sei":
        return False
    n = len(word)
    ok = [False] * (n + 1)
    ok[n] = True
    for i in range(n - 1, -1, -1):
        for piece in _IT_FULL:
            if word.startswith(piece, i) and ok[i + len(piece)]:
                ok[i] = True
                break
        if ok[i]:
            continue
        for piece in _IT_ELIDED:
            j = i + len(piece)
            if word.startswith(piece, i) and j < n and word[j] in "uo" and ok[j]:
                ok[i] = True
                break
        if not ok[i] and i > 0 and word.startswith("un", i) and i + 2 == n:
            ok[i] = True
    return ok[0]


_IT_SUFFIXES = ("esimo", "esima", "esimi", "esime", "ennio", "enni", "ennale", "ennali", "enne",
                "enario", "enaria", "enari", "enarie")
_IT_SPECIAL_STEMS = frozenset({"dec", "bi", "tri", "quadri", "quinqu", "sess", "sett", "mill", "cent"})


def italian_number_derivative(word: str) -> bool:
    """Ordinals and age or period words built on a number: ventesimo, ventunesimo, ventitreesimo,
    ventennio, decennio, trentennale, biennale, ventenne, centenario."""
    if len(word) > MAX_WORD:
        return False
    for suffix in _IT_SUFFIXES:
        if word.endswith(suffix) and len(word) > len(suffix):
            stem = word[: -len(suffix)]
            if stem in _IT_SPECIAL_STEMS or italian_number_word(stem) or stem == "sei":
                return True
            # a stem of three letters is too short to tell apart (ottenne is "obtained", not otto)
            if len(stem) >= 4 and any(italian_number_word(stem + v) for v in "aeio"):
                return True
    return False


def italian_collective(word: str) -> bool:
    """ventina, trentina, quindicina: a number stem plus -ina or -ine. Only a count when "di"
    follows (una trentina di dipendenti; la cucina trentina is not), checked by the caller."""
    for suffix in ("ina", "ine"):
        if word.endswith(suffix) and len(word) > 3:
            stem = word[:-3]
            if any(italian_number_word(stem + v) for v in "aeio"):
                return True
    return False


# Ordinary words that start like a blocking prefix (bestiame is livestock).
PREFIX_EXCEPTIONS = ("bestia", "bestow")

ROMAN = re.compile(r"m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})")
ROMAN_WORDS = _f("mix dix vic civ liv dim mid lid cdi cli div mic mill dill ill vill lil")


def roman_numeral(word: str) -> bool:
    return bool(word) and ROMAN.fullmatch(word) is not None and set(word) <= set("mdclxvi")


# ---------------------------------------------------------------- context words

# Words that are often not a number ("sei" is also "you are", "prima" is also "before", "una" is
# the article, "the one" is a pronoun). They BLOCK only when they count something (see
# `lexical.py`); otherwise they produce nothing, except the few in FLAG_WORDS.
COUNT_WORDS: dict[Language, frozenset[str]] = {
    Language.EN: _f("one first second half double"),
    Language.IT: _f("uno una un sei prima primo secondo"),
    Language.TA: frozenset({"ஒரு", "அரை", "கால்", "இரு", "முதல்"}),
}
# Articles that are also "one": they count only before a measure noun or "sola/solo/unica".
ARTICLE_COUNT_WORDS = frozenset({"un", "uno", "una", "ஒரு"})

# Words that FLAG for the reviewer when they do not count something.
FLAG_WORDS: dict[Language, frozenset[str]] = {
    Language.EN: _f("first only top latest newest guaranteed guarantee"),
    Language.IT: _f("ottimo ottima ottimi ottime massimo massima massimi massime minimo minima minimi minime "
                    "garantito garantita garantiti garantite garantiamo unico unica unici uniche primo primi prime"),
    Language.TA: frozenset(),
}
# Comparatives that FLAG when no "than" or "di" makes them a comparison claim that blocks.
FLAG_COMPARATIVES: dict[Language, frozenset[str]] = {
    Language.EN: _f("better worse cheaper faster quicker bigger larger smaller higher lower stronger safer "
                    "superior inferior"),
    Language.IT: _f("meglio peggio superiore superiori inferiore inferiori maggiore maggiori minore minori"),
    Language.TA: frozenset({"அதிக", "அதிகம்", "அதிகமா", "குறைவான", "குறைந்த", "மேலான"}),
}

# Universal claims, and what makes them a claim: a verb of performance or a product noun in the
# same sentence ("never breaks down", "always the right price"). Otherwise nothing.
UNIVERSAL: dict[Language, frozenset[str]] = {
    Language.EN: _f("never always every all everyone everybody everything nobody"),
    Language.IT: _f("nessuno nessuna sempre mai tutti tutte"),
    Language.TA: frozenset({"எப்போதும்", "ஒருபோதும்", "அனைத்து", "எல்லா"}),  # always, never, all
}
PERFORMANCE: dict[Language, frozenset[str]] = {
    Language.EN: _f("beat beats lasts last breaks break fails fail delivers deliver wins win saves save sells sell "
                    "works outperforms guarantees price prices cost costs quality warranty delivery deliveries "
                    "rate rates discount discounts uptime breakdown breakdowns satisfied satisfaction"),
    Language.IT: _f("batte battiamo vince vinciamo dura durano rompe rompono consegna consegniamo consegne vende "
                    "vendiamo risparmia risparmi funziona funzionano garantisce prezzo prezzi costo costi qualita "
                    "garanzia sconto sconti tasso tassi soddisfazione soddisfatti guasto guasti"),
    Language.TA: frozenset({"விலை", "தரம்", "உத்தரவாதம்"}),
}

# Nouns that make a COUNT_WORD a count: it modifies one, or one names the count before a colon.
# Folded forms. Units and currencies are added from units.py by `lexical.py`.
MEASURE_NOUNS: frozenset[str] = _f(
    "year years month months week weeks day days hour hours minute minutes percent tonne tonnes "
    "anno anni mese mesi settimana settimane giorno giorni ora ore minuto minuti tonnellata tonnellate euro "
    "chilometri chilometro litri litro")
QUANTITY_NOUNS: frozenset[str] = MEASURE_NOUNS | _f(
    "customer customers client clients unit units truck trucks van vans vehicle vehicles model models branch "
    "branches showroom showrooms store stores shop shops dealer dealers dealership dealerships order orders sale "
    "sales people employee employees location locations city cities price prices star stars place "
    "cliente clienti modello modelli negozio negozi veicolo veicoli camion furgone furgoni dipendente dipendenti "
    "persona persone citta ordine ordini vendita vendite filiale filiali concessionaria concessionarie "
    "concessionario concessionari sede sedi salone saloni punto punti prezzo prezzi stella stelle posto")
TIME_NOUNS: frozenset[str] = _f("year day week month edition generation anno giorno edizione generazione")
TA_QUANTITY_STEMS = ("வாடிக்கையாளர", "ஆண்டு", "ஆண்ட", "வருட", "மாத", "வார", "நாள", "நாட்", "கடை", "மணி", "விலை",
                     "கிலோ", "லிட்டர", "டன்", "கிளை", "வண்டி", "வாகன", "லட்ச", "கோடி")
TA_MEASURE_STEMS = ("ஆண்டு", "ஆண்ட", "வருட", "மாத", "வார", "நாள", "நாட்", "மணி", "கிலோ", "லிட்டர", "டன்",
                    "லட்ச", "கோடி")
ROMAN_NEIGHBOURS = _f("year years anno anni anniversary anniversario edition edizione century centuries secolo "
                      "secoli generation generazione place posto chapter capitolo volume")
SOLE = _f("sola solo unica unico singola singolo")
RANK_MARKERS = _f("n no nr num numero number")
COPULAS = _f("is are was sono e era")
# Function words that never stand between a count word and what it counts.
FUNCTION_WORDS = _f(
    "of the a an and or to for with in on at by from our your their its this that these those "
    "di de del della dello dei degli delle da dal dalla in a al alla ai agli alle per con su sul sulla il lo la i "
    "gli le l un una uno e o ed che ne ci si ti mi vi tu io noi voi lui lei loro me te")

# Fixed phrases that never produce a finding (folded regex, wrapped in LEFT, RIGHT).
EXCEPTION_PHRASES: tuple[str, ...] = (
    r"on\s+top\s+of", r"top[-\s]?ups?", r"at\s+first", r"first\s+of\s+all", r"at\s+least(?:\s+once)?",
    r"at\s+most", r"at\s+the\s+very\s+least", r"most\s+of", r"rather\s+than",
    r"other\s+than", r"second[-\s]hand", r"a\s+second",
    r"one[-\s]stop", r"double\s+cab", r"half[-\s]day", r"last\s+but\s+not\s+least", r"not\s+only",
    r"no[-\s]?one", r"one[-\s]of[-\s]a[-\s]kind", r"one\s+another", r"one\s+by\s+one",
    r"(?:two|three|four)-?wheelers?",
    r"la\s+prima\s+volta", r"una\s+volta", r"prima\s+di", r"prima\s+che",
    r"secondo\s+(?:me|te|noi|voi|lui|lei|loro|i|le|il|la|lo|gli|l['’]|quanto|cui)",
    r"un\s+po['’]?",
)


def _term(word: str) -> str:
    return r"\s+".join(re.escape(part) for part in fold_str(word).split())


def _pattern(regex: str) -> str:
    """Drop Latin accents from a lowercase regex so it matches folded text."""
    return unicodedata.normalize("NFC", _LATIN_MARKS.sub("", unicodedata.normalize("NFD", regex)))


def languages_for(language: Language) -> tuple[Language, ...]:
    return (language,) if language is Language.EN else (language, Language.EN)


@cache
def block_patterns(language: Language) -> tuple[tuple[Category, re.Pattern[str]], ...]:
    out: list[tuple[Category, re.Pattern[str]]] = []
    for category in Category:
        parts = [LEFT + _pattern(p) + RIGHT for lang in languages_for(language)
                 for p in BLOCK_PATTERNS[lang].get(category, ())]
        if parts:
            parts.sort(key=len, reverse=True)
            out.append((category, re.compile("|".join(f"(?:{p})" for p in parts))))
    return tuple(out)


@cache
def exception_pattern() -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{LEFT}{_pattern(p)}{RIGHT})" for p in EXCEPTION_PHRASES))


@cache
def merged(table_name: str, language: Language) -> frozenset[str]:
    table = {"count": COUNT_WORDS, "flag": FLAG_WORDS, "comparative": FLAG_COMPARATIVES, "universal": UNIVERSAL,
             "performance": PERFORMANCE}[table_name]
    return frozenset().union(*(table[lang] for lang in languages_for(language)))


@cache
def block_word(word: str, language: Language) -> Category | None:
    """The category of a folded word that blocks on its own, or None."""
    langs = languages_for(language)
    for lang in langs:
        for category, words in BLOCK_WORDS[lang].items():
            if word in words:
                return category
    for lang in langs:
        for category, prefixes in BLOCK_PREFIXES[lang].items():
            if word.startswith(prefixes) and not word.startswith(PREFIX_EXCEPTIONS):
                return category
    if english_number_word(word):
        return Category.NUMBER_WORD
    if language is Language.IT and (italian_number_word(word) or italian_number_derivative(word)):
        return Category.NUMBER_WORD
    if language is Language.TA:
        if any(root in word for root in TA_INFIX):
            return Category.NUMBER_WORD
        for stem in TA_STEMS:
            i = word.find(stem)
            while i >= 0:
                j = i + len(stem)
                if j < len(word) and word[j] in TA_STEM_NEXT:
                    return Category.NUMBER_WORD
                i = word.find(stem, i + 1)
    return None
