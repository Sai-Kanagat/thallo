"""Units and currencies a claim may carry. A unit outside these tables is refused when the claim
is created, so no free text ever reaches the page through a unit (for example "EUR best")."""
from __future__ import annotations

from cortex_operator.contracts.enums import Language

# Symbols, the same in every language.
SYMBOL_UNITS = frozenset({"kg", "g", "km", "m", "cm", "mm", "km/h", "L", "ml", "kW", "kWh", "W", "cc", "hp",
                          "CV", "Nm", "m2", "m3", "km/l"})

# unit code -> language -> (singular, plural)
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

PERCENT = "%"

# ISO 4217 codes accepted for prices: a subset of the standard covering the pilots' regions.
# Add a code here (it must be a real ISO 4217 code) before a claim may use it.
ISO_4217 = frozenset({
    "EUR", "INR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "BGN", "TRY", "AED",
    "SAR", "QAR", "KWD", "BHD", "OMR", "SGD", "MYR", "LKR", "BDT", "NPR", "PKR", "AUD", "NZD", "CAD", "JPY",
    "CNY", "HKD", "THB", "IDR", "PHP", "ZAR", "BRL", "MXN",
})
CURRENCY_SYMBOLS: dict[str, str] = {"EUR": "€", "INR": "₹", "USD": "$", "GBP": "£"}


def is_number_unit(unit: str) -> bool:
    return unit == PERCENT or unit in SYMBOL_UNITS or unit in UNITS


def is_currency(unit: str) -> bool:
    return unit in ISO_4217
