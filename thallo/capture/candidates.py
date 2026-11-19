"""Numbers in a transcript become claim CANDIDATES, never verified claims.

Plain code, no model. A candidate is not usable in copy until a person verifies it through the
truth library (`thallo.truth.verify_claim`, human only); this module never calls that. What it
finds:

- digit numbers, read with the transcript's language: in Italian "1.200" is twelve hundred and
  "3,5" is three and a half; in English and Tamil the other way round. Indian grouping
  (1,00,000) is read in English and Tamil. A number whose separators do not fit the language
  ("1,5" in English) is skipped rather than guessed.
- a unit or currency right after (or a currency sign right before): "%", "per cent", the symbol
  units of `truth/units.py`, unit words in the language (days, giorni), ISO codes, signs and
  currency words. With a currency it is a PRICE, otherwise a NUMBER.
- a four-digit year from 1900 to 2099 with nothing after it is a DATE.
- common number words in English and Italian ("twelve", "dodici"). Words that are also articles
  or pronouns (one, uno, una, un) are skipped. Tamil number words wait for a reviewed lexicon.

Each candidate keeps the exact span of the transcript it came from (`source_span` start, end,
text) and the sha256 of the whole transcript as evidence. Running extraction twice on the same
source adds nothing: a span already on a claim of that source is skipped.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from thallo.contracts.actors import SystemActor
from thallo.contracts.clock import Clock
from thallo.contracts.enums import ClaimKind, Language
from thallo.contracts.events import Event
from thallo.contracts.models import Claim, Source
from thallo.truth.claims import ClaimError, new_candidate, next_code
from thallo.truth.units import CURRENCY_SYMBOLS, ISO_4217, PERCENT, SYMBOL_UNITS, UNITS

ACTOR = SystemActor(id="capture")
MAX_CANDIDATES_PER_SOURCE = 50

_NUM = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+|[  ]\d{3}(?!\d))*(?![\w])")
_SIGNS = {v: k for k, v in CURRENCY_SYMBOLS.items()}
_CURRENCY_WORDS = {
    "euro": "EUR", "euros": "EUR", "rupee": "INR", "rupees": "INR", "rupia": "INR", "rupie": "INR",
    "dollar": "USD", "dollars": "USD", "dollari": "USD", "dollaro": "USD", "pound": "GBP", "pounds": "GBP",
    "sterline": "GBP", "sterlina": "GBP",
}
_PERCENT_WORDS = ("per cent", "percent", "per cento", "percento")
_WORDS: dict[Language, dict[str, int]] = {
    Language.EN: {
        "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
        "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
    },
    Language.IT: {
        "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
        "undici": 11, "dodici": 12, "tredici": 13, "quattordici": 14, "quindici": 15, "sedici": 16,
        "diciassette": 17, "diciotto": 18, "diciannove": 19, "venti": 20, "trenta": 30, "quaranta": 40,
        "cinquanta": 50, "sessanta": 60, "settanta": 70, "ottanta": 80, "novanta": 90, "cento": 100,
        "mille": 1000,
    },
}
_WORD_RE = re.compile(r"\b[^\W\d_]+\b", re.UNICODE)


@dataclass(frozen=True)
class Found:
    kind: ClaimKind
    value: str
    unit: str | None
    start: int
    end: int
    text: str


def _canonical(int_part: str, frac: str) -> str | None:
    if not int_part.isdigit() or (frac and not frac.isdigit()):
        return None
    whole = int_part.lstrip("0") or "0"
    frac = frac.rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def _groups_ok(groups: list[str], indian: bool) -> bool:
    if not 1 <= len(groups[0]) <= 3 or any(len(g) != 3 for g in groups[1:]):
        if not indian or len(groups) < 3:
            return False
        # Indian: 1 or 2 digits, then pairs, then the last group of three (1,00,000; 12,34,567)
        return (1 <= len(groups[0]) <= 2 and all(len(g) == 2 for g in groups[1:-1]) and len(groups[-1]) == 3)
    return True


def parse_number(raw: str, language: Language) -> str | None:
    """The canonical value ("1200", "3.5") of a digit number as written in this language, or None
    when its separators do not fit the language."""
    s = raw.replace(" ", " ")
    italian = language is Language.IT
    dec, grp = (",", ".") if italian else (".", ",")
    if " " in s:  # a space only ever groups thousands
        if dec in s or grp in s:
            return None
        parts = s.split(" ")
        return _canonical("".join(parts), "") if _groups_ok(parts, False) else None
    int_part, sep, frac = s.partition(dec)
    if sep and (dec in frac or grp in frac):
        return None
    if grp in int_part:
        groups = int_part.split(grp)
        if not _groups_ok(groups, indian=not italian):
            return None
        int_part = "".join(groups)
    return _canonical(int_part, frac)


def _unit_after(text: str, end: int, language: Language) -> tuple[str | None, ClaimKind | None, int]:
    """(unit, kind override, new end) for what follows a number."""
    rest = text[end:end + 24]
    m = re.match(r"\s?%", rest)
    if m:
        return PERCENT, None, end + m.end()
    low = rest.lower()
    for word in _PERCENT_WORDS:
        m = re.match(r"\s+" + re.escape(word) + r"\b", low)
        if m:
            return PERCENT, None, end + m.end()
    m = re.match(r"\s?([€₹$£])", rest)
    if m:
        return _SIGNS[m.group(1)], ClaimKind.PRICE, end + m.end()
    m = re.match(r"\s+([A-Z]{3})\b", rest)
    if m and m.group(1) in ISO_4217:
        return m.group(1), ClaimKind.PRICE, end + m.end()
    m = re.match(r"\s+([^\W\d_]+)\b", low)
    if m and m.group(1) in _CURRENCY_WORDS:
        return _CURRENCY_WORDS[m.group(1)], ClaimKind.PRICE, end + m.end()
    for sym in sorted(SYMBOL_UNITS, key=len, reverse=True):
        m = re.match(r"\s?" + re.escape(sym) + r"(?![\w/])", rest)
        if m:
            return sym, None, end + m.end()
    if m := re.match(r"\s+([^\W\d_]+)\b", low):
        word = m.group(1)
        for code, names in UNITS.items():
            if language in names and word in names[language]:
                return code, None, end + m.end()
    return None, None, end


_SIGN_BEFORE = re.compile(r"([€₹$£])\s?$")
_CODE_BEFORE = re.compile(r"(?<![A-Za-z])([A-Z]{3})\s$")


def _sign_before(text: str, start: int) -> tuple[str | None, int]:
    """(currency, where the span starts) for a sign or ISO code right before a number."""
    head = text[max(0, start - 4):start]
    offset = start - len(head)
    if (m := _SIGN_BEFORE.search(head)) is not None:
        return _SIGNS[m.group(1)], offset + m.start()
    if (m := _CODE_BEFORE.search(head)) is not None and m.group(1) in ISO_4217:
        return m.group(1), offset + m.start()
    return None, start


def find_numbers(text: str, language: Language | None) -> list[Found]:
    """Every number this module can read in `text`, in order."""
    lang = language or Language.EN
    found: list[Found] = []
    taken: list[tuple[int, int]] = []
    for m in _NUM.finditer(text):
        raw = m.group(0).rstrip("  ")
        start, end = m.start(), m.start() + len(raw)
        value = parse_number(raw, lang)
        if value is None:
            continue
        currency, span_start = _sign_before(text, start)
        unit, kind, span_end = _unit_after(text, end, lang)
        if currency is not None and unit is None:
            unit, kind = currency, ClaimKind.PRICE
        else:
            span_start = start
        if kind is None:
            if unit is None and re.fullmatch(r"(19|20)\d\d", raw):
                kind = ClaimKind.DATE
            else:
                kind = ClaimKind.NUMBER
        found.append(Found(kind, value, unit, span_start, span_end, text[span_start:span_end]))
        taken.append((span_start, span_end))
    words = _WORDS.get(lang, {})
    for m in _WORD_RE.finditer(text):
        n = words.get(m.group(0).lower())
        if n is None or any(a <= m.start() < b for a, b in taken):
            continue
        unit, kind, span_end = _unit_after(text, m.end(), lang)
        found.append(Found(kind or ClaimKind.NUMBER, str(n), unit, m.start(), span_end, text[m.start():span_end]))
    return sorted(found, key=lambda f: f.start)


class Rows(Protocol):
    def list_all(self, model: type, **where: Any) -> list: ...
    def insert(self, row: Any) -> Any: ...


def extract_candidates(tx: Rows, source: Source, *, clock: Clock,
                       existing: Sequence[Claim] | None = None) -> tuple[list[Claim], list[Event]]:
    """Write a claim candidate for every new number in the source's transcript. Idempotent per span."""
    transcript = source.transcript or ""
    if not transcript.strip():
        return [], []
    claims = list(existing) if existing is not None else tx.list_all(Claim)
    seen = {(c.source_span or {}).get("start") for c in claims if c.source_id == source.id}
    codes: list[str] = [c.code for c in claims]
    made: list[Claim] = []
    events: list[Event] = []
    for f in find_numbers(transcript, source.language)[:MAX_CANDIDATES_PER_SOURCE]:
        if f.start in seen:
            continue
        code = next_code(codes)
        try:
            claim, event = new_candidate(
                tenant_id=source.tenant_id, code=code, kind=f.kind, value=f.value, unit=f.unit, clock=clock,
                actor=ACTOR, source_id=source.id, source_span={"start": f.start, "end": f.end, "text": f.text},
                evidence=transcript)
        except ClaimError:
            continue  # e.g. more decimals than the currency has; a person can still add it by hand
        tx.insert(claim)
        codes.append(code)
        seen.add(f.start)
        made.append(claim)
        events.append(event)
    return made, events


def spans(claims: Iterable[Claim], source_id: str) -> list[dict[str, Any]]:
    return [dict(c.source_span or {}) for c in claims if c.source_id == source_id]


__all__ = ["ACTOR", "Found", "extract_candidates", "find_numbers", "parse_number"]
