"""Slot filling. The writer emits `{{C-0012}}`; code fills the value and unit in the right language.

Precedence for each slot:
1. The claim's approved wording for the language (`claim.languages[lang]`), used verbatim. A human
   approved it when verifying, so it wins over any formatting below.
2. NUMBER and PRICE: the canonical value formatted for the language (decimal separator, digit
   grouping) with the unit rendered from UNITS or CURRENCIES.
3. DATE: "5 January 2026", "5 gennaio 2026", "5 ஜனவரி 2026".
4. NAME: the value verbatim. Names are not translated.
5. QUOTE and CAPABILITY: the value verbatim, only when the copy is in the tenant's default
   language (the language the value was captured and verified in). Otherwise the fill refuses:
   a model translation of a quote or capability would be an unverified claim.

Any unknown or unusable claim refuses the whole fill. Nothing is ever partly filled.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.contracts.models import Claim
from cortex_operator.truth.claims import parse_date_value, problem

SLOT = re.compile(r"\{\{(C-\d{4,})\}\}")


class FillError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# ---------------------------------------------------------------- numbers

SEPARATORS: dict[Language, tuple[str, str]] = {  # (decimal, group)
    Language.EN: (".", ","),
    Language.IT: (",", "."),
    Language.TA: (".", ","),
}


def _group(digits: str, language: Language, sep: str) -> str:
    if len(digits) <= 3:
        return digits
    if language is Language.TA:  # Indian grouping: 12,34,567
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        return sep.join([*parts, tail])
    parts = []
    while digits:
        parts.insert(0, digits[-3:])
        digits = digits[:-3]
    return sep.join(parts)


def format_number(value: str, language: Language) -> str:
    """Format a canonical decimal string ("1234.50") for a language, keeping every given digit."""
    dec, grp = SEPARATORS[language]
    sign = "-" if value.startswith("-") else ""
    body = value.lstrip("-")
    whole, _, frac = body.partition(".")
    out = _group(whole, language, grp)
    return f"{sign}{out}{dec}{frac}" if frac else f"{sign}{out}"


# ---------------------------------------------------------------- units

# unit code -> language -> (singular, plural). Symbols are the same in every language.
SYMBOL_UNITS = frozenset({"kg", "g", "km", "m", "cm", "mm", "km/h", "L", "ml", "kW", "kWh", "W", "cc", "hp",
                          "CV", "Nm", "m2", "m3", "km/l"})
UNITS: dict[str, dict[Language, tuple[str, str]]] = {
    "t": {Language.EN: ("tonne", "tonnes"), Language.IT: ("tonnellata", "tonnellate"),
          Language.TA: ("டன்", "டன்")},
    "hour": {Language.EN: ("hour", "hours"), Language.IT: ("ora", "ore"),
             Language.TA: ("மணி நேரம்", "மணி நேரம்")},
    "day": {Language.EN: ("day", "days"), Language.IT: ("giorno", "giorni"),
            Language.TA: ("நாள்", "நாட்கள்")},
    "week": {Language.EN: ("week", "weeks"), Language.IT: ("settimana", "settimane"),
             Language.TA: ("வாரம்", "வாரங்கள்")},
    "month": {Language.EN: ("month", "months"), Language.IT: ("mese", "mesi"),
              Language.TA: ("மாதம்", "மாதங்கள்")},
    "year": {Language.EN: ("year", "years"), Language.IT: ("anno", "anni"),
             Language.TA: ("ஆண்டு", "ஆண்டுகள்")},
}
CURRENCIES: dict[str, str] = {"EUR": "€", "INR": "₹", "USD": "$", "GBP": "£"}


def _with_unit(number: str, value: str, unit: str | None, language: Language, default: Language) -> str:
    if unit is None:
        return number
    if unit == "%":
        return f"{number}%"
    if unit in SYMBOL_UNITS:
        return f"{number} {unit}"
    forms = UNITS.get(unit, {}).get(language)
    if forms:
        return f"{number} {forms[0] if Decimal(value) == 1 else forms[1]}"
    if language is default:
        return f"{number} {unit}"
    raise FillError([f"no wording for unit {unit!r} in {language.value}; add it to the claim's languages"])


def _price(number: str, unit: str, language: Language) -> str:
    symbol = CURRENCIES.get(unit)
    if language is Language.IT:
        return f"{number} {symbol or unit}"
    return f"{symbol}{number}" if symbol else f"{unit} {number}"


# ---------------------------------------------------------------- dates

MONTHS: dict[Language, tuple[str, ...]] = {
    Language.EN: ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                  "October", "November", "December"),
    Language.IT: ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
                  "settembre", "ottobre", "novembre", "dicembre"),
    Language.TA: ("ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்", "ஜூலை", "ஆகஸ்ட்", "செப்டம்பர்",
                  "அக்டோபர்", "நவம்பர்", "டிசம்பர்"),
}


def format_date(value: str, language: Language) -> str:
    year, month, day = parse_date_value(value)
    if month is None:
        return str(year)
    name = MONTHS[language][month - 1]
    return f"{day} {name} {year}" if day is not None else f"{name} {year}"


# ---------------------------------------------------------------- one claim

def render_claim(claim: Claim, language: Language, *, default_language: Language) -> str:
    approved = claim.languages.get(language.value)
    if approved:
        return unicodedata.normalize("NFC", approved)
    kind = claim.kind
    if kind is ClaimKind.NUMBER:
        return _with_unit(format_number(claim.value, language), claim.value, claim.unit, language,
                          default_language)
    if kind is ClaimKind.PRICE:
        return _price(format_number(claim.value, language), claim.unit or "", language)
    if kind is ClaimKind.DATE:
        return format_date(claim.value, language)
    if kind is ClaimKind.NAME:
        return claim.value
    if language is default_language:
        return claim.value
    raise FillError([f"{claim.code}: no approved {language.value} wording for this {kind.value}"])


# ---------------------------------------------------------------- whole text

@dataclass(frozen=True)
class Filled:
    text: str
    codes: tuple[str, ...]  # in order of first use
    claim_ids: tuple[str, ...]


def fill(text: str, *, language: Language, claims: Mapping[str, Claim], clock: Clock,
         default_language: Language) -> Filled:
    """Replace every `{{C-xxxx}}` slot with the claim's wording. `claims` is keyed by code.
    Run `compile_text` first; `build` does both."""
    text = unicodedata.normalize("NFC", text)
    now: datetime = clock.now()
    problems: list[str] = []
    rendered: dict[str, str] = {}
    order: list[str] = []
    for code in SLOT.findall(text):
        if code in order:
            continue
        order.append(code)
        claim = claims.get(code)
        why = problem(claim, now)
        if why or claim is None:
            problems.append(f"{code}: {why}")
            continue
        try:
            rendered[code] = render_claim(claim, language, default_language=default_language)
        except FillError as exc:
            problems.extend(exc.problems)
    if problems:
        raise FillError(problems)
    out = SLOT.sub(lambda m: rendered[m.group(1)], text)
    if "{{" in out or "}}" in out:
        raise FillError(["malformed slot left after filling"])
    return Filled(text=out, codes=tuple(order), claim_ids=tuple(claims[c].id for c in order))
