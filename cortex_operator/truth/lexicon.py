"""Per-language word lists for the compile step and the sentence segmenter.

Digits are universal and need no list: any character whose Unicode category is Nd, No or Nl
counts (ASCII, Tamil, Devanagari, Arabic-Indic, fullwidth, superscripts, fractions, Roman
numerals). Superlatives, comparatives and number words are per language.

Matching is case-insensitive and uses its own word boundary, because Python's `\\b` treats the
vowel signs of Tamil (and other Indic scripts) as non-word characters and would split words.
A `whole` term must stand alone. A `prefix` term matches the start of a word, which suits
Tamil, where suffixes attach to the stem.

The Tamil lists are a first draft and need a native reader. The Italian and English lists are
deliberately broad: the compile step fails closed, so a false hit costs a rewrite, not an error
in public.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from functools import cache

from cortex_operator.contracts.enums import Language


class Category(StrEnum):
    SUPERLATIVE = "superlative"
    COMPARATIVE = "comparative"
    NUMBER_WORD = "number_word"


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
_LETTERS = rf"(?:[^\W\d_]|[{MARKS}])"  # a letter or a combining mark, no digit or underscore


def is_digit_char(ch: str) -> bool:
    return unicodedata.category(ch) in ("Nd", "No", "Nl")


def is_hidden_char(ch: str) -> bool:
    """Invisible format characters (zero-width space, joiners, BOM, soft hyphen and the like).
    They can split a word so that a list match misses it, so the compile step refuses them."""
    return unicodedata.category(ch) == "Cf"


def is_money_or_percent(ch: str) -> bool:
    return unicodedata.category(ch) == "Sc" or ch in "%‰‱٪﹪％"


@dataclass(frozen=True)
class Lexicon:
    whole: dict[Category, tuple[str, ...]]
    prefix: dict[Category, tuple[str, ...]]
    patterns: dict[Category, tuple[str, ...]]  # raw regex fragments, wrapped in LEFT and RIGHT


_H = r"[-\s]?"  # optional hyphen or space inside a compound

EN = Lexicon(
    whole={
        Category.SUPERLATIVE: (
            "best", "worst", "biggest", "largest", "smallest", "fastest", "quickest", "cheapest", "lowest",
            "highest", "greatest", "strongest", "safest", "newest", "latest", "oldest", "finest", "most",
            "least", "first", "only", "unique", "leading", "number one", "unbeatable", "unmatched",
            "unrivalled", "unrivaled", "unparalleled", "unsurpassed", "guarantee", "guaranteed",
            "guarantees", "proven", "ultimate", "premier",
        ),
        Category.COMPARATIVE: (
            "better", "worse", "cheaper", "faster", "quicker", "bigger", "larger", "smaller", "higher",
            "lower", "stronger", "safer", "superior", "inferior", "than", "compared", "versus", "vs",
            "outperform", "outperforms", "outperformed", "outperforming", "outsell", "outsells",
        ),
        Category.NUMBER_WORD: (
            "zero", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
            "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
            "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
            "hundreds", "thousand", "thousands", "million", "millions", "billion", "billions", "dozen",
            "dozens", "double", "triple", "twice", "thrice", "half", "lakh", "lakhs", "crore", "crores",
        ),
    },
    prefix={},
    patterns={
        Category.SUPERLATIVE: (
            rf"world{_H}class", rf"best{_H}in{_H}class", rf"market{_H}leader", rf"industry{_H}leader",
            rf"top{_H}(?:rated|selling|quality|ranked)",
        ),
    },
)

IT = Lexicon(
    whole={
        Category.SUPERLATIVE: (
            "migliore", "migliori", "peggiore", "peggiori", "ottimo", "ottima", "ottimi", "ottime",
            "pessimo", "pessima", "pessimi", "pessime", "massimo", "massima", "massimi", "massime",
            "minimo", "minima", "minimi", "minime", "primo", "primi", "unico", "unica", "unici", "uniche",
            "leader", "numero uno", "imbattibile", "imbattibili", "ineguagliabile", "ineguagliabili",
            "insuperabile", "insuperabili", "ineguagliato", "ineguagliata", "garantito", "garantita",
            "garantiti", "garantite", "garantiamo",
        ),
        Category.COMPARATIVE: (
            "meglio", "peggio", "superiore", "superiori", "inferiore", "inferiori", "maggiore", "maggiori",
            "minore", "minori",
        ),
        Category.NUMBER_WORD: (
            "zero", "due", "tre", "quattro", "cinque", "sette", "otto", "nove", "dieci", "undici", "dodici",
            "venti", "trenta", "quaranta", "cinquanta", "sessanta", "settanta", "ottanta", "novanta",
            "cento", "duecento", "trecento", "cinquecento", "mille", "duemila", "tremila", "mila",
            "milione", "milioni", "miliardo", "miliardi", "dozzina", "doppio", "doppia", "triplo",
            "tripla", "metà",
        ),
    },
    prefix={},
    patterns={
        Category.SUPERLATIVE: (
            rf"{_LETTERS}+issim[oaie]",  # absolute superlative: velocissimo, convenientissima
            r"(?:il|la|lo|i|gli|le|l['’])\s*(?:più|piu|meno)",  # relative superlative: il più
        ),
        Category.COMPARATIVE: (
            rf"(?:più|piu|meno)\s+{_LETTERS}+\s+(?:di|del|dello|della|dei|degli|delle|dell['’]|che)",
            r"(?:più|piu|meno)\s+(?:di|del|dello|della|dei|degli|delle|che)",
            r"rispetto\s+(?:a|al|allo|alla|ai|agli|alle|all['\u2019])",
            r"(?:in\s+)?confronto\s+(?:a|con|al|allo|alla|ai|agli|alle|all['\u2019])",
        ),
    },
)

# Tamil: first draft, needs a native reader. Prefix terms catch suffixed forms.
TA = Lexicon(
    whole={
        Category.SUPERLATIVE: ("ஒரே", "மட்டுமே", "நம்பர் ஒன்"),
        Category.COMPARATIVE: ("விட", "காட்டிலும்", "அதிக", "அதிகம்"),
        Category.NUMBER_WORD: (
            "ஒன்று", "இரண்டு", "மூன்று", "நான்கு", "ஐந்து", "ஆறு", "ஏழு", "எட்டு", "ஒன்பது", "பத்து",
            "இருபது", "முப்பது", "நாற்பது", "ஐம்பது", "பாதி", "இரட்டை",
        ),
    },
    prefix={
        Category.SUPERLATIVE: (
            "சிறந்த",  # best, excellent
            "மிக",  # very, most: மிகவும், மிகச், மிகப், மிகக்
            "முதலிடம்",  # first place
            "முதன்மை",  # premier, foremost
            "முதல் தர",  # first class
            "முன்னணி",  # leading
            "தனித்துவ",  # unique
            "உத்தரவாத",  # guarantee
            "நிகரற்ற",  # unmatched
            "ஈடு இணையற்ற",  # incomparable
            "உலகத்தர",  # world class
            "அதிகபட்ச",  # maximum
            "குறைந்தபட்ச",  # minimum
        ),
        Category.COMPARATIVE: (
            "விடக்", "விடச்", "விடப்", "விடவும்",  # than, with sandhi
            "ஒப்பிட",  # compared: ஒப்பிடும்போது, ஒப்பிடுகையில்
            "அதிகமா",  # more: அதிகமான, அதிகமாக (not அதிகார, authorised)
            "குறைவான", "குறைந்த",  # less, lower
            "மேலான",  # superior
        ),
        Category.NUMBER_WORD: ("நூறு", "நூற்", "ஆயிர", "லட்ச", "இலட்ச", "கோடி"),
    },
    patterns={},
)

LEXICONS: dict[Language, Lexicon] = {Language.EN: EN, Language.IT: IT, Language.TA: TA}


def _term(word: str) -> str:
    return r"\s+".join(re.escape(part) for part in word.split())


@cache
def matchers(language: Language) -> tuple[tuple[Category, re.Pattern[str]], ...]:
    """Compiled matchers for `language`, plus English, since English words turn up in Italian
    and Tamil copy. Order: superlative, comparative, number word."""
    langs = [language] if language is Language.EN else [language, Language.EN]
    out: list[tuple[Category, re.Pattern[str]]] = []
    for category in Category:
        parts: list[str] = []
        for lang in langs:
            lex = LEXICONS[lang]
            parts += [LEFT + _term(w) + RIGHT for w in lex.whole.get(category, ())]
            parts += [LEFT + _term(w) for w in lex.prefix.get(category, ())]
            parts += [LEFT + p + RIGHT for p in lex.patterns.get(category, ())]
        if parts:
            # longest first so a compound wins over its head word
            parts.sort(key=len, reverse=True)
            out.append((category, re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)))
    return tuple(out)


# ---------------------------------------------------------------- abbreviations for segmentation

_UNIVERSAL_ABBR = frozenset({"e.g", "i.e", "etc", "vs", "rs", "dr", "prof", "no", "nr", "approx", "ca", "fig"})

ABBREVIATIONS: dict[Language, frozenset[str]] = {
    Language.EN: _UNIVERSAL_ABBR | {
        "mr", "mrs", "ms", "sr", "jr", "st", "inc", "ltd", "co", "corp", "nos", "dept", "est", "a.m", "p.m",
        "u.s", "u.k", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    },
    Language.IT: _UNIVERSAL_ABBR | {
        "sig", "sig.ra", "sigg", "dott", "dott.ssa", "ing", "avv", "geom", "rag", "arch", "ecc", "es",
        "p.es", "pag", "pagg", "tel", "cfr", "s.p.a", "s.r.l", "s.n.c", "art", "vol", "gen", "mag", "giu",
        "lug", "ago", "set", "ott", "dic", "c.a", "p.v", "v.le", "p.zza", "c.so", "n",
    },
    Language.TA: _UNIVERSAL_ABBR | {
        "திரு", "திருமதி", "செல்வி", "கி.மீ", "கி.கி", "மி.மீ", "ரூ", "எ.கா", "மு.ப", "பி.ப",
        "mr", "mrs", "ms", "a.m", "p.m",
    },
}
