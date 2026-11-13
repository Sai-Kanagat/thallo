"""Per-language word lists and character rules for the compile step.

Digits are universal and need no list: any character whose Unicode category is Nd, No or Nl
counts (ASCII, Tamil, Devanagari, Arabic-Indic, fullwidth, superscripts, fractions, Roman numeral
characters). Superlatives, comparatives and number words are per language, and English is always
checked too, since English words turn up in Italian and Tamil copy.

Lookalikes. Every word and name matcher runs on a folded view of the text: each character goes
through NFKC (fullwidth, mathematical letters, ligatures, long s), casefold, and loses Latin
combining marks. Letters from scripts a language does not use (Cyrillic, Greek, small capitals,
IPA) are refused outright by `foreign_letter`, so a Cyrillic "е" inside "best" fails the build
instead of slipping past the list.

Matching uses its own word boundary, because Python's `\\b` treats the vowel signs of Tamil as
non-word characters and would split words. A `whole` term must stand alone, a `prefix` term
matches the start of a word, an `infix` term matches anywhere inside one (Tamil number roots).

The Tamil lists are a first draft and need a native reader. All lists are deliberately broad: the
compile step fails closed, so a false hit costs a rewrite, not an error in public.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache

from cortex_operator.contracts.enums import Language


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

_EXTRA_HIDDEN = frozenset({0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}
                          | set(range(0xFE00, 0xFE10)) | set(range(0xE0100, 0xE01F0)))


def is_digit_char(ch: str) -> bool:
    return unicodedata.category(ch) in ("Nd", "No", "Nl")


def is_hidden_char(ch: str) -> bool:
    """Invisible characters that can split or disguise a word: format characters (zero-width
    space, joiners, BOM, soft hyphen), the combining grapheme joiner, variation selectors, Hangul
    fillers and the blank Braille pattern."""
    return unicodedata.category(ch) == "Cf" or ord(ch) in _EXTRA_HIDDEN


def is_money_or_percent(ch: str) -> bool:
    return unicodedata.category(ch) == "Sc" or ch in "%‰‱٪﹪％"


_LATIN_MARKS = re.compile("[̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-︯]")
_LATIN_LETTERS = ((0x41, 0x5A), (0x61, 0x7A), (0xAA, 0xAA), (0xBA, 0xBA), (0xC0, 0x24F))
_TAMIL = (0x0B80, 0x0BFF)


def fold_char(ch: str) -> str:
    """NFKC, casefold, then drop Latin combining marks. May return "" or several characters."""
    out = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", ch).casefold())
    return unicodedata.normalize("NFC", _LATIN_MARKS.sub("", out))


def fold(text: str) -> tuple[str, list[int]]:
    """The folded text and, for each folded character, the index of the original character."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        for f in fold_char(ch):
            chars.append(f)
            origin.append(i)
    return "".join(chars), origin


def fold_str(text: str) -> str:
    return fold(text)[0]


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


# ---------------------------------------------------------------- word lists

@dataclass(frozen=True)
class Lexicon:
    whole: dict[Category, tuple[str, ...]]
    prefix: dict[Category, tuple[str, ...]] = field(default_factory=dict)
    infix: dict[Category, tuple[str, ...]] = field(default_factory=dict)
    patterns: dict[Category, tuple[str, ...]] = field(default_factory=dict)  # regex, wrapped in LEFT, RIGHT


_H = r"[-\s]?"
_EN_UNITS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
             "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
             "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand")

EN = Lexicon(
    whole={
        Category.SUPERLATIVE: (
            "best", "worst", "biggest", "largest", "smallest", "fastest", "quickest", "cheapest", "lowest",
            "highest", "greatest", "strongest", "safest", "newest", "latest", "oldest", "finest", "most",
            "least", "first", "only", "unique", "leading", "number one", "unbeatable", "unmatched",
            "unrivalled", "unrivaled", "unparalleled", "unsurpassed", "guarantee", "guaranteed",
            "guarantees", "proven", "ultimate", "premier", "top", "record", "bestseller", "bestsellers",
            "bestselling",
        ),
        Category.COMPARATIVE: (
            "better", "worse", "cheaper", "faster", "quicker", "bigger", "larger", "smaller", "higher",
            "lower", "stronger", "safer", "superior", "inferior", "than", "compared", "versus", "vs",
            "outperform", "outperforms", "outperformed", "outperforming", "outsell", "outsells",
        ),
        Category.NUMBER_WORD: (
            *_EN_UNITS, "zero", "hundreds", "thousands", "million", "millions", "billion", "billions",
            "dozen", "dozens", "double", "triple", "twice", "thrice", "half", "lakh", "lakhs", "crore",
            "crores", "percent", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
            "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
            "seventeenth", "eighteenth", "nineteenth", "twentieth", "thirtieth", "fortieth", "fiftieth",
            "hundredth", "thousandth", "millionth", "mln", "mld", "mrd", "bn", "mn",
        ),
    },
    patterns={
        Category.SUPERLATIVE: (
            rf"world{_H}class", rf"best{_H}in{_H}class", rf"market{_H}leader", rf"industry{_H}leader",
        ),
        Category.NUMBER_WORD: (
            rf"(?:{'|'.join(_EN_UNITS)}|many|multi){_H}fold",
            r"per\s+cent",
            # Roman numerals of two or more letters, any case, minus everyday words
            r"(?!(?:mi|di|vi|ci|li|mix|dix|vic|civ|liv|dim|mid|lid)(?![\w]))"
            r"(?=[mdclxvi]{2,}(?![\w]))m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})",
        ),
    },
)

_IT_ROOTS = ("undici", "dodici", "tredici", "quattordici", "quindici", "sedici", "diciassette", "diciotto",
             "diciannove", "dieci", "quattr", "cinqu", "sett", "otto", "nove", "vent", "trent", "quarant",
             "cinquant", "sessant", "settant", "ottant", "novant", "cento", "cent", "mille", "mila", "uno",
             "un", "due", "tre", "tré", "sei")

IT = Lexicon(
    whole={
        Category.SUPERLATIVE: (
            "migliore", "migliori", "peggiore", "peggiori", "ottimo", "ottima", "ottimi", "ottime",
            "pessimo", "pessima", "pessimi", "pessime", "massimo", "massima", "massimi", "massime",
            "minimo", "minima", "minimi", "minime", "primo", "primi", "prima", "prime", "unico", "unica",
            "unici", "uniche", "leader", "numero uno", "imbattibile", "imbattibili", "ineguagliabile",
            "ineguagliabili", "insuperabile", "insuperabili", "ineguagliato", "ineguagliata", "garantito",
            "garantita", "garantiti", "garantite", "garantiamo",
        ),
        Category.COMPARATIVE: (
            "meglio", "peggio", "superiore", "superiori", "inferiore", "inferiori", "maggiore", "maggiori",
            "minore", "minori",
        ),
        Category.NUMBER_WORD: (
            "zero", "milione", "milioni", "miliardo", "miliardi", "doppio", "doppia", "triplo", "tripla",
            "metà", "mezzo", "mezza", "percento",
        ),
    },
    prefix={Category.NUMBER_WORD: ("migliai", "centinai", "decin", "dozzin")},
    patterns={
        Category.SUPERLATIVE: (
            rf"{_LETTERS}+issim[oaie]",
            r"(?:il|la|lo|i|gli|le|l['’])\s*(?:più|piu|meno)",
        ),
        Category.COMPARATIVE: (
            rf"(?:più|piu|meno)\s+{_LETTERS}+\s+(?:di|del|dello|della|dei|degli|delle|dell['’]|che)",
            r"(?:più|piu|meno)\s+(?:di|del|dello|della|dei|degli|delle|che)",
            r"rispetto\s+(?:a|al|allo|alla|ai|agli|alle|all['’])",
            r"(?:in\s+)?confronto\s+(?:a|con|al|allo|alla|ai|agli|alle|all['’])",
        ),
        Category.NUMBER_WORD: (
            r"(?:second|terz|quart|quint|sest|settim|ottav|non|decim)[oaie]",
            r"per\s+cento",
        ),
    },
)

# Tamil: first draft, needs a native reader.
TA = Lexicon(
    whole={
        Category.SUPERLATIVE: ("ஒரே", "மட்டுமே", "நம்பர் ஒன்"),
        Category.COMPARATIVE: ("விட", "காட்டிலும்", "அதிக", "அதிகம்"),
        Category.NUMBER_WORD: ("பாதி", "இரட்டை"),
    },
    prefix={
        Category.SUPERLATIVE: (
            "சிறந்த", "மிக", "முதலிடம்", "முதன்மை", "முதல் தர", "முன்னணி", "தனித்துவ", "உத்தரவாத",
            "நிகரற்ற", "ஈடு இணையற்ற", "உலகத்தர", "அதிகபட்ச", "குறைந்தபட்ச",
        ),
        Category.COMPARATIVE: (
            "விடக்", "விடச்", "விடப்", "விடவும்", "ஒப்பிட", "அதிகமா", "குறைவான", "குறைந்த", "மேலான",
        ),
    },
    infix={
        # number roots anywhere in a word: பதினைந்து, இருநூறு, எழுபது, ஆயிரக்கணக்கான.
        # பதின (teens) rather than பதி, so பதிவு (registration) is not caught.
        Category.NUMBER_WORD: (
            "பதின", "பத்து", "பது", "நூறு", "நூற்", "ஆயிர", "லட்ச", "கோடி", "ஒன்று", "இரண்டு", "மூன்று",
            "நான்கு", "ஐந்து", "ஆறு", "ஏழு", "எட்டு", "ஒன்பது",
        ),
    },
)

LEXICONS: dict[Language, Lexicon] = {Language.EN: EN, Language.IT: IT, Language.TA: TA}

# Italian words built only from number roots (trentacinque, ventitré, duemilacinquecento) are
# found by `italian_number_word`, a memoised parse of one word at a time: no regex, so no
# catastrophic backtracking. "un" alone is the article and is left out.
_IT_ROOTS_FOLDED = tuple(sorted({fold_str(r) for r in _IT_ROOTS}, key=len, reverse=True))
_IT_LINK_VOWELS = frozenset("aeiou")
WORD = re.compile(rf"(?:[^\W\d_]|[{MARKS}])+")  # a word, Tamil vowel signs included
MAX_WORD = 64


def italian_number_word(word: str) -> bool:
    """True if a folded word is made only of Italian number roots, each optionally followed by one
    vowel. Linear in the word length times the number of roots."""
    if word == "un" or not word or len(word) > MAX_WORD:
        return False
    ok = [False] * (len(word) + 1)
    ok[len(word)] = True
    for i in range(len(word) - 1, -1, -1):
        for root in _IT_ROOTS_FOLDED:
            if word.startswith(root, i):
                j = i + len(root)
                if ok[j] or (j < len(word) and word[j] in _IT_LINK_VOWELS and ok[j + 1]):
                    ok[i] = True
                    break
    return ok[0]


# ---------------------------------------------------------------- review flags

# Words that are often not a number or a claim at all ("sei" is also "you are", "prima" is also
# "before", "una" is the article, "the one" is a pronoun). They raise a review flag instead of a
# block, unless they sit next to a digit, a slot, a unit or a quantity noun, where they are a count.
AMBIGUOUS: dict[Language, frozenset[str]] = {
    Language.EN: frozenset({"one"}),
    Language.IT: frozenset({"sei", "uno", "una", "prima", "secondo"}),
    Language.TA: frozenset(),
}

# Universal claims ("never breaks down", "every customer"): always a review flag, never a block.
UNIVERSAL: dict[Language, tuple[str, ...]] = {
    Language.EN: ("never", "always", "every", "all", "everyone", "everybody", "everything"),
    Language.IT: ("nessuno", "nessuna", "sempre", "mai", "tutti", "tutte"),
    # Tamil, needs a native reader: always, never, all
    Language.TA: ("எப்போதும்", "ஒருபோதும்", "அனைத்து", "எல்லா"),
}

# Nouns that make a nearby ambiguous word a count. Folded forms. Units and currencies are added
# from units.py by the compiler.
QUANTITY_NOUNS: frozenset[str] = frozenset(fold_str(w) for w in (
    "customer", "customers", "client", "clients", "year", "years", "month", "months", "week", "weeks", "day",
    "days", "hour", "hours", "unit", "units", "truck", "trucks", "vehicle", "vehicles", "model", "models",
    "branch", "branches", "showroom", "showrooms", "store", "stores", "shop", "shops", "dealer", "dealers",
    "dealership", "dealerships", "order", "orders", "sale", "sales", "people", "employee", "employees",
    "location", "locations", "city", "cities", "time", "times", "percent", "tonne", "tonnes",
    "cliente", "clienti", "anno", "anni", "mese", "mesi", "settimana", "settimane", "giorno", "giorni", "ora",
    "ore", "sede", "sedi", "modello", "modelli", "negozio", "negozi", "veicolo", "veicoli", "camion",
    "mezzo", "mezzi", "dipendente", "dipendenti", "persona", "persone", "città", "ordine", "ordini",
    "vendita", "vendite", "filiale", "filiali", "concessionaria", "concessionarie", "concessionario",
    "concessionari", "volta", "volte", "tonnellata", "tonnellate", "euro", "punto", "punti",
    "வாடிக்கையாளர்", "வாடிக்கையாளர்கள்", "ஆண்டு", "ஆண்டுகள்", "மாதம்", "நாள்", "நாட்கள்", "கடை", "கடைகள்",
))

# Product categories that contain a number word but state no claim. Only the closed or hyphenated
# compound: "three wheelers" with a space is a count and stays checkable.
EXCEPTIONS = re.compile(LEFT + r"(?:two|three|four)-?wheelers?" + RIGHT)


def _term(word: str) -> str:
    return r"\s+".join(re.escape(part) for part in fold_str(word).split())


def _pattern(regex: str) -> str:
    """Drop Latin accents from a lowercase regex so it matches folded text. No casefold: that
    would turn the escape \\W into \\w."""
    return unicodedata.normalize("NFC", _LATIN_MARKS.sub("", unicodedata.normalize("NFD", regex)))


@cache
def matchers(language: Language) -> tuple[tuple[Category, re.Pattern[str]], ...]:
    """Compiled matchers for `language` plus English, to run on folded text (see `fold`)."""
    langs = [language] if language is Language.EN else [language, Language.EN]
    out: list[tuple[Category, re.Pattern[str]]] = []
    for category in Category:
        parts: list[str] = []
        for lang in langs:
            lex = LEXICONS[lang]
            parts += [LEFT + _term(w) + RIGHT for w in lex.whole.get(category, ())]
            parts += [LEFT + _term(w) for w in lex.prefix.get(category, ())]
            parts += [_term(w) for w in lex.infix.get(category, ())]
            parts += [LEFT + _pattern(p) + RIGHT for p in lex.patterns.get(category, ())]
        if parts:
            parts.sort(key=len, reverse=True)
            out.append((category, re.compile("|".join(f"(?:{p})" for p in parts))))
    return tuple(out)
