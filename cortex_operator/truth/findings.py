"""What the compile step reports: one `Finding` per problem, with a kind, a span and a severity."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    BLOCK = "block"  # fails the build
    FLAG = "flag"  # passes the build, must be acknowledged by the human reviewer


class FindingKind(StrEnum):
    # structural layer (complete, always BLOCK)
    DIGIT = "digit"
    SYMBOL = "symbol"
    ENCLOSED_DIGIT = "enclosed_digit"
    FOREIGN_SCRIPT = "foreign_script"
    HIDDEN_CHARACTER = "hidden_character"
    TEXT_TOO_LONG = "text_too_long"
    MALFORMED_SLOT = "malformed_slot"
    SLOT_ADJACENT = "slot_adjacent"
    UNKNOWN_CLAIM = "unknown_claim"
    UNUSABLE_CLAIM = "unusable_claim"  # not verified, expired or without a named verifier
    STALE_CLAIM = "stale_claim"  # not the latest version of its code
    UNBOUND_NUMBER = "unbound_number"  # a unitless NUMBER without approved wording for the language
    FOREIGN_CURRENCY = "foreign_currency"  # a PRICE in another currency than the tenant's
    CLAIM_NOT_ALLOWED = "claim_not_allowed"
    # lexical layer (best effort)
    NUMBER_WORD = "number_word"
    AMBIGUOUS_NUMBER_WORD = "ambiguous_number_word"
    UNIVERSAL_CLAIM = "universal_claim"
    SUPERLATIVE = "superlative"
    COMPARATIVE = "comparative"
    NAMED_CUSTOMER = "named_customer"
    SPACED_LETTERS = "spaced_letters"
    # backstop
    CLASSIFIER = "classifier"  # raised by a plugged-in Detector


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    start: int
    end: int
    match: str
    message: str
    sentence: int | None = None
    severity: Severity = Severity.BLOCK
