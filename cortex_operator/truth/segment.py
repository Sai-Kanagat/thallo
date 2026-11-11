"""Sentence segmentation that respects decimals, abbreviations and claim markers, and fails closed.

The legacy splitter cut on every period, so "3.5 tonnes [[C-0001]]." became "3." plus
"5 tonnes [[C-0001]].", and the first fragment was flagged as unsupported (BUILD_PLAN 0.1).

The legacy gate checks each sentence for its own marker, so a wrong merge would let an unmarked
sentence ride on its neighbour's marker. When in doubt this splitter therefore produces MORE
fragments, never fewer. Rules, in order:
- A line break always ends a sentence.
- A run of terminators (. ! ? and the ellipsis) ends a sentence, except:
  - a period between two digits (a decimal: "3.5");
  - a terminator inside a URL or email address detected explicitly;
  - an internal period of a listed abbreviation ("e.g", "Sig.ra", "S.p.A", the Tamil km);
  - a single period after a known abbreviation or an initial, and only when the next visible
    character is a lowercase letter or a digit ("e.g. spanners", "no. 7"). Before an uppercase
    letter, an uncased letter (Tamil) or anything else, it splits ("parts etc. We sold").
- A fragment that holds only claim markers joins the sentence before it, and only when it sits
  on the same line, right after that sentence.

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
    return token.casefold() in ABBREVIATIONS[language] or _is_initial(token)


def _next_visible(text: str, j: int) -> str:
    while j < len(text) and text[j].isspace():
        j += 1
    return text[j] if j < len(text) else ""


URL_OR_EMAIL = re.compile(
    r"(?:https?://|www\.)[^\s<>\"]*[^\s<>\".,;:!?)\]]"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[a-z]{2,}"
    r"|(?<![\w.@])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|it|in|org|net|io|eu|info|biz|co\.in|co\.uk)"
    r"(?:/[^\s<>\"]*[^\s<>\".,;:!?)\]])?(?![\w])"
)


def _protected(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in URL_OR_EMAIL.finditer(text)]


def _token_at(text: str, i: int) -> str:
    """The whitespace-delimited token around position i, without outer quotes, brackets and
    trailing terminators, casefolded."""
    a = i
    while a > 0 and not text[a - 1].isspace():
        a -= 1
    b = i
    while b < len(text) and not text[b].isspace():
        b += 1
    return text[a:b].lstrip(OPENERS).rstrip(CLOSERS + TERMINATORS).casefold()


def _holds_together(text: str, i: int, j: int, run: str, language: Language,
                    protected: list[tuple[int, int]]) -> bool:
    """True when the terminator run at text[i:j] (closers included up to j) must NOT split."""
    if any(a <= i and i + 1 < b for a, b in protected):
        return True  # inside an explicitly detected URL or email
    if run != ".":
        return False
    if 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
        return True  # decimal
    if j < len(text) and not text[j].isspace():
        # an internal period of a listed abbreviation ("e.g", "sig.ra", "s.p.a", the Tamil km)
        token = _token_at(text, i)
        return "." in token and token in ABBREVIATIONS[language]
    if _is_abbreviation(text, i, language):
        nxt = _next_visible(text, j)
        return bool(nxt) and (nxt.isdigit() or (nxt.islower() and nxt.lower() != nxt.upper()))
    return False


def segment(text: str, language: Language = Language.EN) -> list[Sentence]:
    protected = _protected(text)
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
        if _holds_together(text, i, j, run, language, protected):
            i = i + 1 if run == "." else j
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
        if spans and MARKER_ONLY.match(text[a:b]) and "\n" not in text[spans[-1][1]:a]:
            spans[-1] = (spans[-1][0], b)
            continue
        spans.append((a, b))
    return [Sentence(k, a, b, text[a:b]) for k, (a, b) in enumerate(spans)]


def sentence_at(sentences: list[Sentence], offset: int) -> int | None:
    for s in sentences:
        if s.start <= offset < s.end:
            return s.index
    return None
