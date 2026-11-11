"""The compile step (BUILD_PLAN 2.7, "Facts: make errors impossible to type").

The writer's text carries slots like `{{C-0012}}`. Outside a slot the text may not contain:
- a digit of any script (Unicode Nd, No, Nl), or a currency or percent sign
- a spelled-out number word
- a superlative or comparative (per-language lists in `lexicon.py`, English always included)
- a named customer (every NAME claim's value and approved wordings, plus `protected_names`)
- an invisible format character, which could hide any of the above
- a malformed slot or a legacy `[[C-...]]` reference

Every slot must name a claim that exists, is verified, has not expired and, when the brief limits
them, is in `allowed_claim_ids`. Any finding fails the build. Findings carry the span, the
sentence and a reason, so the writer can be asked to fix exactly that.

The model classifier for superlatives and named entities is out of scope here. It plugs in through
`Detector`: anything with a `detect` method returning findings.

Spans refer to `CompileResult.text`, the input in Unicode NFC form.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.contracts.models import Claim
from cortex_operator.truth.claims import problem
from cortex_operator.truth.fill import SLOT, Filled, fill
from cortex_operator.truth.lexicon import (
    EXCEPTIONS,
    LEFT,
    RIGHT,
    is_digit_char,
    is_hidden_char,
    is_money_or_percent,
    matchers,
)
from cortex_operator.truth.segment import Sentence, segment, sentence_at


class FindingKind(StrEnum):
    DIGIT = "digit"
    SYMBOL = "symbol"
    NUMBER_WORD = "number_word"
    SUPERLATIVE = "superlative"
    COMPARATIVE = "comparative"
    NAMED_CUSTOMER = "named_customer"
    HIDDEN_CHARACTER = "hidden_character"
    MALFORMED_SLOT = "malformed_slot"
    UNKNOWN_CLAIM = "unknown_claim"
    UNUSABLE_CLAIM = "unusable_claim"  # not verified, expired or without a named verifier
    CLAIM_NOT_ALLOWED = "claim_not_allowed"
    CLASSIFIER = "classifier"  # raised by a plugged-in Detector


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    start: int
    end: int
    match: str
    message: str
    sentence: int | None = None


@runtime_checkable
class Detector(Protocol):
    """Typed hook for the model classifier (superlatives, comparisons, named entities). It sees
    the text with slots blanked out, so it can never flag a claim's own value."""

    def detect(self, masked_text: str, sentences: Sequence[Sentence], language: Language) -> Sequence[Finding]:
        ...


@dataclass(frozen=True)
class CompileResult:
    text: str
    language: Language
    sentences: tuple[Sentence, ...]
    codes: tuple[str, ...]  # slot codes, in order of first use
    claim_ids: tuple[str, ...]  # ids of the claims those codes resolved to
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.findings

    def errors(self) -> list[str]:
        return [f"{f.kind.value}: {f.message}" for f in self.findings]


MALFORMED = re.compile(r"\{\{|\}\}|\[\[|\]\]|[{\[]\s*C-\d+")


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


def _runs(text: str, test: Callable[[str], bool]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        if test(text[i]):
            j = i
            while j < len(text) and test(text[j]):
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def protected_names_from(claims: Iterable[Claim], extra: Iterable[str] = ()) -> list[str]:
    """Every name that may appear only through a slot: NAME claims in any state, their approved
    wordings, and any extra names (customers, leads) the caller passes."""
    names: set[str] = set()
    for claim in claims:
        if claim.kind is ClaimKind.NAME:
            names.add(claim.value)
            names.update(claim.languages.values())
    names.update(extra)
    return sorted({unicodedata.normalize("NFC", n).strip() for n in names if len(n.strip()) >= 2},
                  key=len, reverse=True)


def compile_text(text: str, *, language: Language, claims: Mapping[str, Claim], clock: Clock,
                 allowed_claim_ids: Collection[str] | None = None, protected_names: Iterable[str] = (),
                 detectors: Sequence[Detector] = ()) -> CompileResult:
    """Check writer output before any slot is filled. `claims` is keyed by code and should hold
    the tenant's whole library, so that NAME claims protect their names even when unused."""
    text = unicodedata.normalize("NFC", text)
    now = clock.now()
    sentences = segment(text, language)
    findings: list[Finding] = []

    def add(kind: FindingKind, a: int, b: int, message: str) -> None:
        findings.append(Finding(kind, a, b, text[a:b], message, sentence_at(sentences, a)))

    # slots
    codes: list[str] = []
    slot_spans: list[tuple[int, int]] = []
    for m in SLOT.finditer(text):
        slot_spans.append(m.span())
        code = m.group(1)
        claim = claims.get(code)
        if claim is None:
            add(FindingKind.UNKNOWN_CLAIM, *m.span(), f"{code} is not in the truth library")
        else:
            why = problem(claim, now)
            if why:
                add(FindingKind.UNUSABLE_CLAIM, *m.span(), why)
            elif allowed_claim_ids is not None and claim.id not in allowed_claim_ids:
                add(FindingKind.CLAIM_NOT_ALLOWED, *m.span(), f"{code} is not allowed by this brief")
        if code not in codes:
            codes.append(code)

    masked = _blank(text, slot_spans)

    for m in MALFORMED.finditer(masked):
        add(FindingKind.MALFORMED_SLOT, *m.span(), "malformed slot; write claims as {{C-0001}}")
    for a, b in _runs(masked, is_hidden_char):
        add(FindingKind.HIDDEN_CHARACTER, a, b, "invisible formatting character")
    for a, b in _runs(masked, is_digit_char):
        add(FindingKind.DIGIT, a, b, f"digits outside a slot: {text[a:b]!r}")
    for a, b in _runs(masked, is_money_or_percent):
        add(FindingKind.SYMBOL, a, b, f"currency or percent sign outside a slot: {text[a:b]!r}")

    taken: list[tuple[int, int]] = [m.span() for m in EXCEPTIONS.finditer(masked)]
    for category, pattern in matchers(language):
        kind = FindingKind(category.value)
        for m in pattern.finditer(masked):
            a, b = m.span()
            if any(a < y and x < b for x, y in taken):
                continue
            taken.append((a, b))
            add(kind, a, b, f"{kind.value} outside a slot: {m.group(0)!r}")

    names = protected_names_from(claims.values(), protected_names)
    if names:
        name_re = re.compile("|".join(LEFT + r"\s+".join(map(re.escape, n.split())) + RIGHT for n in names),
                             re.IGNORECASE)
        for m in name_re.finditer(masked):
            add(FindingKind.NAMED_CUSTOMER, *m.span(), f"name outside a slot: {m.group(0)!r}")

    for detector in detectors:
        findings.extend(detector.detect(masked, sentences, language))

    findings.sort(key=lambda f: (f.start, f.end, f.kind.value))
    claim_ids = tuple(claims[c].id for c in codes if c in claims)
    return CompileResult(text=text, language=language, sentences=tuple(sentences), codes=tuple(codes),
                         claim_ids=claim_ids, findings=tuple(findings))


class CompileFailed(ValueError):
    def __init__(self, result: CompileResult) -> None:
        super().__init__("; ".join(result.errors()))
        self.result = result


@dataclass(frozen=True)
class Built:
    compiled: CompileResult
    filled: Filled


def build(text: str, *, language: Language, claims: Mapping[str, Claim], clock: Clock,
          default_language: Language, allowed_claim_ids: Collection[str] | None = None,
          protected_names: Iterable[str] = (), detectors: Sequence[Detector] = ()) -> Built:
    """Compile, then fill. Raises CompileFailed or FillError; never returns half-checked text."""
    result = compile_text(text, language=language, claims=claims, clock=clock,
                          allowed_claim_ids=allowed_claim_ids, protected_names=protected_names,
                          detectors=detectors)
    if not result.ok:
        raise CompileFailed(result)
    return Built(compiled=result, filled=fill(result.text, language=language, claims=claims, clock=clock,
                                              default_language=default_language))
