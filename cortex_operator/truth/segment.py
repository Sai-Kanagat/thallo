"""Sentence segmentation that respects decimals, abbreviations and claim markers.

The legacy splitter cut on every period, so "3.5 tonnes [[C-0001]]." became "3." plus
"5 tonnes [[C-0001]].", and the first fragment was flagged as unsupported (BUILD_PLAN 0.1).

Rules, in order:
- A line break always ends a sentence (social copy often has no final period).
- A run of terminators (. ! ? and the ellipsis) ends a sentence only when followed, after any
  closing quotes or brackets, by whitespace or the end of the text. So "3.5", "3,5",
  "example.com" and "S.p.A" never split.
- A single period does not end a sentence after a known abbreviation for the language, after an
  initial (one letter, or one Tamil syllable), or after a token with an internal period ("e.g").
- A single period followed by a lowercase Latin letter does not end a sentence.
- A fragment that holds only claim markers ("{{C-0001}}" or "[[C-0001]]") joins the sentence
  before it.

Offsets refer to the text as given, so callers can point at the exact span.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from cortex_operator.contracts.enums import Language
from cortex_operator.truth.lexicon import ABBREVIATIONS

TERMINATORS = ".!?…।。"
CLOSERS = "\"')]}»”’"
OPENERS = "\"'([{«“‘"
MARKER_ONLY = re.compile(r"^(?:\s*(?:\{\{\s*C-\d+\s*\}\}|\[\[\s*C-\d+\s*\]\])\s*[.!?]*)+\s*$")


@dataclass(frozen=True)
class Sentence:
    index: int
    start: int
    end: int
    text: str


def _token_before(text: str, dot: int) -> str:
    i = dot
    while i > 0 and not text[i - 1].isspace():
        i -= 1
    return text[i:dot].lstrip(OPENERS)


def _is_initial(token: str) -> bool:
    """One letter, optionally followed by combining marks (a Tamil consonant plus vowel sign)."""
    if not token:
        return False
    if unicodedata.category(token[0])[0] != "L":
        return False
    return all(unicodedata.category(c)[0] == "M" for c in token[1:])


def _is_abbreviation(text: str, dot: int, language: Language) -> bool:
    token = _token_before(text, dot)
    if not token:
        return False
    if "." in token and not token.endswith("."):
        return True  # internal period: e.g, S.p.A, கி.மீ
    return token.casefold() in ABBREVIATIONS[language] or _is_initial(token)


def _next_visible(text: str, j: int) -> str:
    while j < len(text) and text[j].isspace():
        j += 1
    return text[j] if j < len(text) else ""


def segment(text: str, language: Language = Language.EN) -> list[Sentence]:
    cuts: list[tuple[int, int]] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            cuts.append((start, i))
            start = i = i + 1
            continue
        if ch not in TERMINATORS:
            i += 1
            continue
        j = i
        while j < n and text[j] in TERMINATORS:
            j += 1
        run = text[i:j]
        while j < n and text[j] in CLOSERS:
            j += 1
        if j < n and not text[j].isspace():
            i = j  # decimal, URL, internal period
            continue
        if run == ".":
            if _is_abbreviation(text, i, language):
                i = j
                continue
            nxt = _next_visible(text, j)
            if nxt and nxt.islower() and unicodedata.name(nxt, "").startswith("LATIN"):
                i = j
                continue
        cuts.append((start, j))
        start = i = j
    cuts.append((start, n))

    spans: list[tuple[int, int]] = []
    for a0, b0 in cuts:
        a, b = a0, b0
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a == b:
            continue
        if spans and MARKER_ONLY.match(text[a:b]):
            spans[-1] = (spans[-1][0], b)
            continue
        spans.append((a, b))
    return [Sentence(k, a, b, text[a:b]) for k, (a, b) in enumerate(spans)]


def sentence_at(sentences: list[Sentence], offset: int) -> int | None:
    for s in sentences:
        if s.start <= offset < s.end:
            return s.index
    return None
