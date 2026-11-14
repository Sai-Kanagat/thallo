"""The slop catalogue: banned constructions per language (BUILD_PLAN 2.7, "Copy: source it, then
strip it"). Applied to every candidate before selection; a hit counts against the candidate.

The built-in lists are a starting set for English and Italian, written for this package and not
yet calibrated on reviewer edits. Tamil has no built-in list yet: its patterns need a native
reviewer. A brand adds "the phrases reviewers have already removed" through `extra_phrases`.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from thallo.contracts.enums import Language


class SlopKind(StrEnum):
    EMPTY_OPENER = "empty_opener"
    FALSE_CONTRAST = "false_contrast"
    TRIAD = "triad"
    HYPE = "hype"
    REMOVED_BEFORE = "removed_before"  # a phrase this brand's reviewers already cut


@dataclass(frozen=True)
class SlopHit:
    kind: SlopKind
    start: int
    end: int
    match: str


def _words(*phrases: str) -> str:
    return r"\b(?:" + "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in phrases) + r")\b"


_EN: dict[SlopKind, list[str]] = {
    SlopKind.EMPTY_OPENER: [
        r"^\s*(?:in\s+today'?s\s+(?:fast[\s-]paced|digital|ever[\s-]changing)\s+(?:world|landscape|age))",
        r"^\s*(?:are\s+you\s+(?:looking|ready|tired)\b)",
        r"^\s*(?:imagine\s+(?:a\s+world|if)\b)",
        r"^\s*(?:have\s+you\s+ever\s+wondered\b)",
        _words("look no further", "without further ado", "let's dive in", "let us dive in"),
    ],
    SlopKind.FALSE_CONTRAST: [
        r"\b(?:it'?s|this\s+is|isn'?t|is)\s+not\s+just\b[^.!?]{0,60}?[,;]?\s*(?:it'?s|but)\b",
        r"\bisn'?t\s+just\b[^.!?]{0,60}?\b(?:it'?s|but)\b",
        r"\bmore\s+than\s+just\b",
    ],
    SlopKind.TRIAD: [
        r"\b[A-Za-z]{3,14},\s+[A-Za-z]{3,14},?\s+and\s+[A-Za-z]{3,14}[.!]",
    ],
    SlopKind.HYPE: [
        _words("game-changing", "game changer", "revolutionary", "revolutionize", "cutting-edge", "seamless",
               "seamlessly", "unlock", "unleash", "elevate", "next-level", "world-class", "state-of-the-art",
               "delve", "tapestry", "in the realm of", "supercharge", "synergy", "best-in-class",
               "take it to the next level", "transformative"),
    ],
}

_IT: dict[SlopKind, list[str]] = {
    SlopKind.EMPTY_OPENER: [
        r"^\s*(?:nel\s+mondo\s+(?:di\s+oggi|frenetico|digitale))",
        r"^\s*(?:sei\s+(?:alla\s+ricerca|pronto|stanco)\b)",
        r"^\s*(?:immagina\s+(?:un\s+mondo|se)\b)",
        _words("non cercare oltre", "senza ulteriori indugi"),
    ],
    SlopKind.FALSE_CONTRAST: [
        r"\bnon\s+(?:è\s+)?solo\b[^.!?]{0,60}?\bma\s+(?:anche\s+)?\b",
        r"\bpiù\s+che\s+(?:un|una|uno)\s+semplice\b",
    ],
    SlopKind.TRIAD: [
        r"\b[A-Za-zÀ-ÿ]{3,14},\s+[A-Za-zÀ-ÿ]{3,14},?\s+e\s+[A-Za-zÀ-ÿ]{3,14}[.!]",
    ],
    SlopKind.HYPE: [
        _words("rivoluzionario", "rivoluzionaria", "all'avanguardia", "innovativo", "innovativa", "eccellenza",
               "senza precedenti", "sinergia", "a trecentosessanta gradi", "scopri come", "unico nel suo genere",
               "esperienza unica"),
    ],
}

CATALOGUE: dict[Language, dict[SlopKind, list[re.Pattern[str]]]] = {
    lang: {k: [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in ps] for k, ps in table.items()}
    for lang, table in ((Language.EN, _EN), (Language.IT, _IT), (Language.TA, {}))
}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("’", "'")


def find_slop(text: str, language: Language, extra_phrases: Iterable[str] = ()) -> list[SlopHit]:
    """Every catalogue hit in `text`, in order. `extra_phrases` are plain phrases (not regexes)."""
    folded = _fold(text)
    hits: list[SlopHit] = []
    for kind, patterns in CATALOGUE[language].items():
        for pattern in patterns:
            hits += [SlopHit(kind, m.start(), m.end(), m.group()) for m in pattern.finditer(folded)]
    for raw in extra_phrases:
        phrase = _fold(raw).strip()
        if phrase:
            pattern = re.compile(_words(phrase), re.IGNORECASE)
            hits += [SlopHit(SlopKind.REMOVED_BEFORE, m.start(), m.end(), m.group()) for m in pattern.finditer(folded)]
    return sorted(hits, key=lambda h: (h.start, h.end, h.kind.value))
