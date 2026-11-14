"""Sentence segmentation, for display and writer feedback only.

This is not a security boundary. The compile step decides from spans alone (`compiler.scan` and
the slot checks) and only uses sentences to tell the writer where a finding sits. A test swaps
this function for "whole text" and "every character" and checks that no verdict changes.

Rules:
- Every Unicode line boundary ends a sentence (the `str.splitlines` set: \\n, \\r, \\v, \\f,
  \\x1c to \\x1e, \\x85, U+2028, U+2029).
- A run of terminators (. ! ? and the ellipsis), plus any closing quotes or brackets, ends a
  sentence, except a single period between two lowercase letters or digits ("3.5",
  "example.com"), which fixes the BUILD_PLAN 0.1 bug where "3.5 tonnes [[C-0001]]." split.
- A fragment holding only claim markers joins the sentence before it when on the same line.

Offsets refer to the text as given.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

from thallo.contracts.enums import Language

TERMINATORS = ".!?…।。"
CLOSERS = "\"')]}»”’"
LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
MARKER_ONLY = re.compile(r"^(?:\s*(?:\{\{\s*C-[0-9]+\s*\}\}|\[\[\s*C-[0-9]+\s*\]\])\s*[.!?]*)+\s*$")


@dataclass(frozen=True)
class Sentence:
    index: int
    start: int
    end: int
    text: str


def _lower_alnum(ch: str) -> bool:
    return ch.isdigit() or (ch.islower() and ch.isalpha())


def segment(text: str, language: Language = Language.EN) -> list[Sentence]:
    cuts: list[tuple[int, int]] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in LINE_BREAKS:
            cuts.append((start, i))
            start = i = i + 1
            continue
        if ch not in TERMINATORS:
            i += 1
            continue
        if ch == "." and 0 < i < n - 1 and _lower_alnum(text[i - 1]) and _lower_alnum(text[i + 1]):
            i += 1
            continue
        j = i
        while j < n and text[j] in TERMINATORS:
            j += 1
        while j < n and text[j] in CLOSERS:
            j += 1
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
        gap = text[spans[-1][1]:a] if spans else ""
        if spans and MARKER_ONLY.match(text[a:b]) and not (set(gap) & LINE_BREAKS):
            spans[-1] = (spans[-1][0], b)
            continue
        spans.append((a, b))
    return [Sentence(k, a, b, text[a:b]) for k, (a, b) in enumerate(spans)]


def sentence_at(sentences: list[Sentence], offset: int) -> int | None:
    """Index of the sentence holding `offset`, by binary search over the sorted starts."""
    k = bisect_right([s.start for s in sentences], offset) - 1
    if k >= 0 and sentences[k].start <= offset < sentences[k].end:
        return sentences[k].index
    return None
