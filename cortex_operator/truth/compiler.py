"""The compile step (BUILD_PLAN 2.7, "Facts: make errors impossible to type").

The writer's text carries slots like `{{C-0012}}`. Outside a slot the text may not contain:
- a digit of any script (Unicode Nd, No, Nl), or a currency or percent sign
- a number word, including compounds (trentacinque, பதினைந்து), ordinals, Roman numerals and
  multipliers (fivefold, mln)
- a superlative or comparative (per-language lists in `lexicon.py`, English always included)
- a named customer (every NAME claim's value and approved wordings, plus `protected_names`)
- a letter from a script the language does not use (Cyrillic or Greek lookalikes, small capitals)
- an invisible character, or letters spaced out to dodge the lists ("b e s t")
- a malformed slot or a legacy `[[C-...]]` reference

Two verdict tiers. A BLOCK finding fails the build. A FLAG finding passes the build and goes to
the human reviewer: ambiguous words that are often not a number ("sei", "una", "prima", "the
one") and universal claims ("never", "always", "every", "sempre"). An ambiguous word next to a
digit, a slot, a unit or a quantity noun ("sei anni", "one truck") is a count and blocks.

Word and name lists run on a folded view (NFKC, casefold, no Latin accents), so fullwidth,
mathematical and ligature lookalikes match too.

Every slot must name a claim of this tenant that exists, is verified, has not expired and, when
the brief limits them, is in `allowed_claim_ids`. A slot must stand alone: the characters right
beside it may only be whitespace, punctuation or the text edge; it may not join another slot
through . , ' : / or -; and the next word may not be a multiplier (k, M, mln, x, fold) or a
symbol (the multiplication sign). Otherwise code could print a number nobody verified
("{{C-0001}}{{C-0002}}" prints 105).

Segmentation is not a security boundary. The verdict is span-based (`scan` plus the slot checks);
the segmenter only labels each finding with the sentence to show the writer. `build` then checks
the filled output: every digit must sit inside one rendered slot, or the build fails.

Text longer than MAX_TEXT_CHARS is refused before any pattern runs.

The model classifier and the second reader of another model family are backstops that plug in
through `Detector`; they are not the guard.

Spans refer to `CompileResult.text`, the input in Unicode NFC form.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable

from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.enums import CheckLayer, CheckVerdict, ClaimKind, Language
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import CheckResult, Claim
from cortex_operator.truth.claims import problem
from cortex_operator.truth.fill import SLOT, Filled, FillError, _fill, check_library
from cortex_operator.truth.lexicon import (
    AMBIGUOUS,
    EXCEPTIONS,
    LEFT,
    MARKS,
    QUANTITY_NOUNS,
    RIGHT,
    UNIVERSAL,
    WORD,
    Category,
    fold,
    fold_str,
    foreign_letter,
    is_digit_char,
    is_hidden_char,
    is_money_or_percent,
    italian_number_word,
    matchers,
)
from cortex_operator.truth.segment import Sentence, sentence_at
from cortex_operator.truth.segment import segment as _segment  # display only; tests swap it
from cortex_operator.truth.units import ISO_4217, SYMBOL_UNITS, UNITS

MAX_TEXT_CHARS = 20_000


class Severity(StrEnum):
    BLOCK = "block"  # fails the build
    FLAG = "flag"  # passes the build, shown to the human reviewer


class FindingKind(StrEnum):
    DIGIT = "digit"
    SYMBOL = "symbol"
    NUMBER_WORD = "number_word"
    AMBIGUOUS_NUMBER_WORD = "ambiguous_number_word"
    UNIVERSAL_CLAIM = "universal_claim"
    SUPERLATIVE = "superlative"
    COMPARATIVE = "comparative"
    NAMED_CUSTOMER = "named_customer"
    FOREIGN_SCRIPT = "foreign_script"
    HIDDEN_CHARACTER = "hidden_character"
    SPACED_LETTERS = "spaced_letters"
    TEXT_TOO_LONG = "text_too_long"
    MALFORMED_SLOT = "malformed_slot"
    SLOT_ADJACENT = "slot_adjacent"
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
    severity: Severity = Severity.BLOCK


@runtime_checkable
class Detector(Protocol):
    """Typed hook for the model classifier (superlatives, comparisons, named entities). It sees
    the text with slots blanked out, so it can never flag a claim's own value. A backstop only:
    the token checks above decide on their own."""

    def detect(self, masked_text: str, sentences: Sequence[Sentence], language: Language) -> Sequence[Finding]:
        ...


@dataclass(frozen=True)
class CompileResult:
    text: str
    language: Language
    tenant_id: str
    sentences: tuple[Sentence, ...]
    codes: tuple[str, ...]  # slot codes, in order of first use
    claim_ids: tuple[str, ...]  # ids of the claims those codes resolved to
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def blocks(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCK)

    @property
    def flags(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.FLAG)

    @property
    def ok(self) -> bool:
        return not self.blocks

    def errors(self) -> list[str]:
        return [f"{f.kind.value}: {f.message}" for f in self.blocks]


MALFORMED = re.compile(r"\{\{|\}\}|\[\[|\]\]|[{\[]\s*C-\d+")
SPACED = re.compile(r"(?<![\w])(?:[^\W\d_][\s.\-_*·]+){2,}[^\W\d_](?![\w])")
SLOT_JOINERS = ".,'’:/-‐‑‒"
MULTIPLIERS = frozenset({"k", "m", "mn", "bn", "mln", "mld", "mio", "mrd", "cr", "x", "fold"})
NAME_SPLIT = re.compile(rf"(?:[^\w{MARKS}]|_)+")
NAME_GAP = rf"(?:[^\w{MARKS}]|_)*"  # "Rossi Trasporti", "Rossi-Trasporti", "RossiTrasporti"
_UNIT_WORDS = frozenset(
    {fold_str(u) for u in SYMBOL_UNITS} | {fold_str(c) for c in ISO_4217}
    | {fold_str(w) for forms in UNITS.values() for pair in forms.values() for w in pair}
)
COUNT_NEIGHBOURS = QUANTITY_NOUNS | _UNIT_WORDS


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
    return sorted({n.strip() for n in names if len(n.strip()) >= 2}, key=len, reverse=True)


def _name_pattern(names: Sequence[str]) -> re.Pattern[str] | None:
    parts = []
    for name in names:
        words = [re.escape(w) for w in NAME_SPLIT.split(fold_str(name)) if w]
        if words:
            parts.append(LEFT + NAME_GAP.join(words) + RIGHT)
    return re.compile("|".join(sorted(parts, key=len, reverse=True))) if parts else None


COUNT_WINDOW_AFTER = 3
COUNT_WINDOW_BEFORE = 1


def _tokens(raw: str, markers: Sequence[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """Tokens of the original text: ("slot", ...), ("digit", ...), ("money", ...) or a folded word.
    A position inside a marker is a slot before anything else is tested."""
    out: list[tuple[int, int, str]] = []
    i = 0
    n = len(raw)
    while i < n:
        slot = next(((x, y) for x, y in markers if x <= i < y), None)
        if slot is not None:
            out.append((slot[0], slot[1], "\x00slot"))
            i = slot[1]
            continue
        ch = raw[i]
        if is_digit_char(ch):
            out.append((i, i + 1, "\x00digit"))
        elif is_money_or_percent(ch):
            out.append((i, i + 1, "\x00money"))
        elif unicodedata.category(ch)[0] in "LM":
            j = i
            while j < n and unicodedata.category(raw[j])[0] in "LM" \
                    and not any(x <= j < y for x, y in markers):
                j += 1
            out.append((i, j, fold_str(raw[i:j])))
            i = j
            continue
        i += 1
    return out


def _is_count(token: str) -> bool:
    return token.startswith("\x00") or token in COUNT_NEIGHBOURS


def _neighbours_count(raw: str, start: int, end: int, markers: Sequence[tuple[int, int]],
                      tokens: list[tuple[int, int, str]] | None = None) -> bool:
    """True when the word at raw[start:end] reads as a count: a slot, digit, currency or percent
    sign, unit or quantity noun is the word just before it or one of the next three."""
    toks = tokens if tokens is not None else _tokens(raw, markers)
    idx = next((k for k, (x, y, _) in enumerate(toks) if x <= start < y), None)
    if idx is None:
        return False
    window = toks[max(0, idx - COUNT_WINDOW_BEFORE):idx] + toks[idx + 1:idx + 1 + COUNT_WINDOW_AFTER]
    return any(_is_count(t) for _, _, t in window)


def scan(text: str, *, language: Language, marker_spans: Iterable[tuple[int, int]] = (),
         protected_names: Iterable[str] = ()) -> list[Finding]:
    """The gate's token check. Blanks the given marker spans, then flags every checkable token
    left anywhere in the text. It never looks at sentence boundaries: a marker covers only its own
    span, so segmentation cannot change the verdict. Findings carry no sentence index."""
    if len(text) > MAX_TEXT_CHARS:
        return [Finding(FindingKind.TEXT_TOO_LONG, 0, 0, "", f"text is over {MAX_TEXT_CHARS} characters")]
    markers = list(marker_spans)
    masked = _blank(text, markers)
    findings: list[Finding] = []

    def add(kind: FindingKind, a: int, b: int, message: str, severity: Severity = Severity.BLOCK) -> None:
        findings.append(Finding(kind, a, b, text[a:b], message, severity=severity))

    for m in MALFORMED.finditer(masked):
        add(FindingKind.MALFORMED_SLOT, *m.span(), "malformed slot; write claims as {{C-0001}}")
    for a, b in _runs(masked, is_hidden_char):
        add(FindingKind.HIDDEN_CHARACTER, a, b, "invisible character")
    for a, b in _runs(masked, is_digit_char):
        add(FindingKind.DIGIT, a, b, f"digits outside a slot: {text[a:b]!r}")
    for a, b in _runs(masked, is_money_or_percent):
        add(FindingKind.SYMBOL, a, b, f"currency or percent sign outside a slot: {text[a:b]!r}")
    for a, b in _runs(masked, lambda c: foreign_letter(c, language)):
        add(FindingKind.FOREIGN_SCRIPT, a, b, f"letters from another script: {text[a:b]!r}")

    folded, origin = fold(masked)

    def span(a: int, b: int) -> tuple[int, int]:
        return origin[a], origin[b - 1] + 1

    for m in SPACED.finditer(folded):
        add(FindingKind.SPACED_LETTERS, *span(*m.span()), f"spaced-out letters: {m.group(0)!r}")

    # word lists: (category, folded span), first match wins on overlap
    hits: list[tuple[Category, int, int]] = []
    for category, pattern in matchers(language):
        hits += [(category, *m.span()) for m in pattern.finditer(folded) if m.end() > m.start()]
    if language is Language.IT:
        hits += [(Category.NUMBER_WORD, *m.span()) for m in WORD.finditer(folded)
                 if italian_number_word(m.group(0))]
    taken: list[tuple[int, int]] = [m.span() for m in EXCEPTIONS.finditer(folded)]
    ambiguous = AMBIGUOUS[language] | AMBIGUOUS[Language.EN]
    tokens = _tokens(text, markers)
    for category, a, b in hits:
        if any(a < y and x < b for x, y in taken):
            continue
        taken.append((a, b))
        word = folded[a:b]
        if word in ambiguous and not _neighbours_count(text, origin[a], origin[b - 1] + 1, markers, tokens):
            add(FindingKind.AMBIGUOUS_NUMBER_WORD, *span(a, b),
                f"may state a number: {word!r}; check it is not a count", Severity.FLAG)
            continue
        kind = FindingKind(category.value)
        add(kind, *span(a, b), f"{kind.value} outside a slot: {word!r}")

    universal = {fold_str(w) for lang in {language, Language.EN} for w in UNIVERSAL[lang]}
    for m in WORD.finditer(folded):
        a, b = m.span()
        word = m.group(0)
        if word in universal or (language is Language.TA and any(word.startswith(w) for w in universal)):
            if not any(a < y and x < b for x, y in taken):
                add(FindingKind.UNIVERSAL_CLAIM, *span(a, b), f"universal claim: {word!r}", Severity.FLAG)

    name_re = _name_pattern(list(protected_names))
    if name_re is not None:
        for m in name_re.finditer(folded):
            add(FindingKind.NAMED_CUSTOMER, *span(*m.span()), f"name outside a slot: {m.group(0)!r}")
    return findings


def _allowed_beside_slot(ch: str) -> bool:
    return ch == "" or ch.isspace() or unicodedata.category(ch)[0] == "P"


def _slot_findings(text: str, slots: list[tuple[int, int]]) -> list[Finding]:
    """A slot must stand alone, so code can never print a number nobody verified."""
    out: list[Finding] = []
    starts = {a for a, _ in slots}
    for a, b in slots:
        before = text[a - 1] if a > 0 else ""
        after = text[b] if b < len(text) else ""
        nxt = text[b:b + 40].lstrip()
        next_char = nxt[:1]
        word = WORD.match(fold_str(nxt))
        next_word = word.group(0) if word else ""
        why = None
        if b in starts:
            why = "slot touches the next slot"
        elif after and after in SLOT_JOINERS and b + 1 in starts:
            why = f"slot joined to the next slot by {after!r}"
        elif not _allowed_beside_slot(before):
            why = f"slot touches {before!r} before it"
        elif not _allowed_beside_slot(after):
            why = f"slot touches {after!r} after it"
        elif next_char and unicodedata.category(next_char)[0] == "S":
            why = f"symbol after a slot: {next_char!r}"
        elif next_word in MULTIPLIERS:
            why = f"multiplier after a slot: {next_word!r}"
        if why:
            out.append(Finding(FindingKind.SLOT_ADJACENT, a, b, text[a:b], why))
    return out


def compile_text(text: str, *, tenant_id: str, language: Language, claims: Mapping[str, Claim], clock: Clock,
                 allowed_claim_ids: Collection[str] | None = None, protected_names: Iterable[str] = (),
                 detectors: Sequence[Detector] = ()) -> CompileResult:
    """Check writer output before any slot is filled. `claims` is this tenant's library of latest
    versions keyed by code (`repository.library`), so that NAME claims protect their names even
    when unused. A claim of another tenant, or under a key that is not its code, raises
    LibraryError."""
    check_library(claims, tenant_id)
    text = unicodedata.normalize("NFC", text)
    now = clock.now()
    findings: list[Finding] = []
    if len(text) > MAX_TEXT_CHARS:
        findings = scan(text, language=language)
        return CompileResult(text=text, language=language, tenant_id=tenant_id, sentences=(), codes=(),
                             claim_ids=(), findings=tuple(findings))

    codes: list[str] = []
    slot_spans: list[tuple[int, int]] = []
    for m in SLOT.finditer(text):
        slot_spans.append(m.span())
        code = m.group(1)
        claim = claims.get(code)
        a, b = m.span()
        if claim is None:
            findings.append(Finding(FindingKind.UNKNOWN_CLAIM, a, b, text[a:b], f"{code} is not in the truth library"))
        else:
            why = problem(claim, now)
            if why:
                findings.append(Finding(FindingKind.UNUSABLE_CLAIM, a, b, text[a:b], why))
            elif allowed_claim_ids is not None and claim.id not in allowed_claim_ids:
                findings.append(Finding(FindingKind.CLAIM_NOT_ALLOWED, a, b, text[a:b],
                                        f"{code} is not allowed by this brief"))
        if code not in codes:
            codes.append(code)

    findings += _slot_findings(text, slot_spans)
    findings += scan(text, language=language, marker_spans=slot_spans,
                     protected_names=protected_names_from(claims.values(), protected_names))

    # display only: which sentence to show the writer
    sentences = _segment(text, language)
    if detectors:
        masked = _blank(text, slot_spans)
        for detector in detectors:
            findings.extend(detector.detect(masked, sentences, language))
    findings = [replace(f, sentence=sentence_at(sentences, f.start)) for f in findings]
    findings.sort(key=lambda f: (f.start, f.end, f.kind.value))
    claim_ids = tuple(claims[c].id for c in codes if c in claims)
    return CompileResult(text=text, language=language, tenant_id=tenant_id, sentences=tuple(sentences),
                         codes=tuple(codes), claim_ids=claim_ids, findings=tuple(findings))


class CompileFailed(ValueError):
    def __init__(self, result: CompileResult) -> None:
        super().__init__("; ".join(result.errors()))
        self.result = result


@dataclass(frozen=True)
class Built:
    compiled: CompileResult
    filled: Filled

    @property
    def flags(self) -> tuple[Finding, ...]:
        """For the human reviewer: ambiguous number words and universal claims."""
        return self.compiled.flags


CHECK_NAME = "truth.compile"


def to_check_results(result: CompileResult, *, variant_id: str, clock: Clock, attempt: int = 1,
                     model_id: str | None = None) -> list[CheckResult]:
    """One `check_result` row (layer CODE) per BLOCK finding (verdict FAIL) and per FLAG finding
    (verdict FLAG, which the approver must acknowledge one by one), or a single PASS row. The
    reason carries the kind, the span and the message, so the review card can point at the text."""
    rows = []
    for f in result.findings:
        verdict = CheckVerdict.FAIL if f.severity is Severity.BLOCK else CheckVerdict.FLAG
        rows.append(CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=result.tenant_id,
                                variant_id=variant_id, layer=CheckLayer.CODE, check_name=f"{CHECK_NAME}.{f.kind.value}",
                                verdict=verdict, reason=f"[{f.start}:{f.end}] {f.message}", attempt=attempt,
                                model_id=model_id))
    if not rows:
        rows.append(CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=result.tenant_id,
                                variant_id=variant_id, layer=CheckLayer.CODE, check_name=CHECK_NAME,
                                verdict=CheckVerdict.PASS, attempt=attempt, model_id=model_id))
    return rows


def check_filled(filled: Filled) -> None:
    """Every digit in the output must lie inside one rendered slot."""
    for a, b in _runs(filled.text, is_digit_char):
        if not any(x <= a and b <= y for x, y in filled.slot_spans):
            raise FillError([f"digits outside a slot after filling: {filled.text[a:b]!r}"])


def build(text: str, *, tenant_id: str, language: Language, claims: Mapping[str, Claim], clock: Clock,
          default_language: Language, allowed_claim_ids: Collection[str] | None = None,
          protected_names: Iterable[str] = (), detectors: Sequence[Detector] = ()) -> Built:
    """Compile, fill, then check the output. The only public way to fill slots. Raises
    CompileFailed, FillError or LibraryError; never returns half-checked text. Review flags pass
    through on `Built.flags`."""
    result = compile_text(text, tenant_id=tenant_id, language=language, claims=claims, clock=clock,
                          allowed_claim_ids=allowed_claim_ids, protected_names=protected_names,
                          detectors=detectors)
    if not result.ok:
        raise CompileFailed(result)
    filled = _fill(result.text, language=language, claims=claims, clock=clock, default_language=default_language,
                   tenant_id=tenant_id)
    check_filled(filled)
    return Built(compiled=result, filled=filled)
