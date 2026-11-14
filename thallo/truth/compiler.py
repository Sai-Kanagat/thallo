"""The compile step (BUILD_PLAN 2.7, "Facts: make errors impossible to type").

The writer's text carries slots like `{{C-0012}}`. Two layers check it, kept apart in code
(docs/contracts.md, "What the truth compile step guarantees"):

STRUCTURAL (this module, complete, always BLOCK, blind to `allowed_terms`):
- no digit of any script (Unicode Nd, No, Nl), currency or percent sign, keycap or enclosed digit
  (U+1F100 to U+1F1E5, U+1F51F, U+1F4AF, U+1F947 to U+1F949, U+2460 to U+24FF, U+2776 to U+2793,
  the keycap combiner U+20E3), invisible character or letter of another script outside a slot
- S1: between two consecutive slots there is at least one letter, so slots cannot combine
  ("{{C-0001}} {{C-0002}}" would print 10 500)
- S2: a slot touches no letter or symbol; after it (hyphens and dashes skipped) comes no
  multiplier or scale word (times, volte, k, mln, lakh, fold, plus, th ...); before it no sign or
  rank marker (# No + - plus-minus times, no., n., numero, number, nr)
- S3: after filling, every digit lies inside one rendered slot and no enclosed digit is printed
- S4: a unitless NUMBER slot needs approved wording for the language, so the writer cannot choose
  what the number counts
- S5: a slot fills only from the tenant's latest live claim version. Pass `library(repo, tenant)`;
  a version older than another row of the same code given in `claims` is refused, and so is any
  claim not verified, expired or (with `tenant_currency`) a price in another currency that its
  approved wording does not name
- slots are well formed and name a claim of this tenant, allowed by the brief

LEXICAL (`lexical.py`, best effort, tuned for precision): number words, superlatives,
comparatives, universal claims and names. See that module for the rules.

FAIL blocks the build; FLAG passes it and must be acknowledged by the approver one by one.
Segmentation is display only: verdicts are span based. Text longer than MAX_TEXT_CHARS is refused
before any pattern runs. The model classifier and the second reader plug in through `Detector`.

Spans refer to `CompileResult.text`, the input in Unicode NFC form.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from thallo.contracts.clock import Clock
from thallo.contracts.enums import CheckLayer, CheckVerdict, ClaimKind, Language
from thallo.contracts.ids import new_id
from thallo.contracts.models import CheckResult, Claim
from thallo.truth.claims import problem
from thallo.truth.fill import SLOT, Filled, FillError, LibraryError, _fill, check_library
from thallo.truth.findings import AllowedTermError, Finding, FindingKind, Severity
from thallo.truth.lexical import check_allowed_terms, scan_lexical
from thallo.truth.lexicon import (
    STARS,
    WORD,
    fold_str,
    foreign_letter,
    is_digit_char,
    is_enclosed_digit,
    is_hidden_char,
    is_money_or_percent,
)
from thallo.truth.segment import Sentence, sentence_at
from thallo.truth.segment import segment as _segment  # display only; tests swap it

__all__ = ["AllowedTermError", "Built", "CompileFailed", "CompileResult", "Detector", "Finding", "FindingKind",
           "Severity", "build",
           "check_filled", "compile_text", "scan", "to_check_results"]

MAX_TEXT_CHARS = 20_000


@runtime_checkable
class Detector(Protocol):
    """Typed hook for the model classifier (superlatives, comparisons, named entities). It sees
    the text with slots blanked out, so it can never flag a claim's own value. A backstop only."""

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
MULTIPLIERS = frozenset(fold_str(w) for w in (
    "times volte b bln bil mil lac lacs lakh lakhs grand plus x fold k m mn bn mln mld mio mrd cr crore crores "
    "st nd rd th mila milioni million millions billion billions thousand thousands hundred hundreds dozen "
    "percent pc pct").split())
RANK_BEFORE = frozenset({"no", "n", "nr", "num", "numero", "number", "x"})
SIGNS_BEFORE = frozenset("#+" + "".join(map(chr, (0x2116, 0xB1, 0xD7, 0x2795, 0x2716, 0x2715))))
DASHES = frozenset("-" + "".join(map(chr, (*range(0x2010, 0x2016), 0x2212, 0xFE63, 0xFF0D))))


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def _runs(text: str, test: Callable[[str], bool]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if test(text[i]):
            j = i
            while j < n and test(text[j]):
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


def _structural(text: str, markers: Sequence[tuple[int, int]], language: Language) -> list[Finding]:
    masked = _blank(text, markers)
    out: list[Finding] = []

    def add(kind: FindingKind, a: int, b: int, message: str) -> None:
        out.append(Finding(kind, a, b, text[a:b], message))

    for m in MALFORMED.finditer(masked):
        add(FindingKind.MALFORMED_SLOT, *m.span(), "malformed slot; write claims as {{C-0001}}")
    for a, b in _runs(masked, is_hidden_char):
        add(FindingKind.HIDDEN_CHARACTER, a, b, "invisible character")
    for a, b in _runs(masked, is_digit_char):
        add(FindingKind.DIGIT, a, b, f"digits outside a slot: {text[a:b]!r}")
    for a, b in _runs(masked, is_enclosed_digit):
        add(FindingKind.ENCLOSED_DIGIT, a, b, f"enclosed or keycap digit outside a slot: {text[a:b]!r}")
    for a, b in _runs(masked, is_money_or_percent):
        add(FindingKind.SYMBOL, a, b, f"currency or percent sign outside a slot: {text[a:b]!r}")
    for a, b in _runs(masked, lambda c: c in STARS):
        if b - a >= 2:
            add(FindingKind.SYMBOL, a, b, "a row of rating stars outside a slot")
    for a, b in _runs(masked, lambda c: foreign_letter(c, language)):
        add(FindingKind.FOREIGN_SCRIPT, a, b, f"letters from another script: {text[a:b]!r}")
    return out


def scan(text: str, *, language: Language, marker_spans: Iterable[tuple[int, int]] = (),
         protected_names: Iterable[str] = (), allowed_terms: Iterable[str] = ()) -> list[Finding]:
    """The token check of both layers, span based, on text with the given marker spans treated
    as slots. Never looks at sentence boundaries. Findings carry no sentence index."""
    if len(text) > MAX_TEXT_CHARS:
        return [Finding(FindingKind.TEXT_TOO_LONG, 0, 0, "", f"text is over {MAX_TEXT_CHARS} characters")]
    markers = sorted(marker_spans)
    names = list(protected_names)
    allowed_terms = list(allowed_terms)
    check_allowed_terms(allowed_terms, names)
    findings = _structural(text, markers, language)
    for kind, severity, a, b, message in scan_lexical(text, markers, language, names, allowed_terms):
        findings.append(Finding(kind, a, b, text[a:b], message, severity=severity))
    return findings


def _allowed_beside_slot(ch: str) -> bool:
    return ch == "" or ch.isspace() or unicodedata.category(ch)[0] == "P"


def _word_after(text: str, b: int) -> str:
    i = b
    while i < len(text) and (text[i].isspace() or text[i] in DASHES):
        i += 1
    m = WORD.match(fold_str(text[i:i + 24]))
    return m.group(0) if m else ""


def _before(text: str, a: int) -> str | None:
    """Why the text right before a slot makes a sign or rank, or None."""
    i = a
    if i and text[i - 1] in DASHES:
        return f"sign before a slot: {text[i - 1]!r}"
    while i and text[i - 1] in " \t" + chr(0xA0) + chr(0x202F):
        i -= 1
    if i and text[i - 1] in SIGNS_BEFORE:
        return f"sign or rank marker before a slot: {text[i - 1]!r}"
    j = i
    if j and text[j - 1] == ".":
        j -= 1
    k = j
    while k and WORD.match(text[k - 1]):
        k -= 1
    word = fold_str(text[k:j])
    if word in RANK_BEFORE and (k == 0 or not WORD.match(text[k - 1])):
        return f"rank marker before a slot: {text[k:j]!r}"
    return None


def _slot_findings(text: str, slots: list[tuple[int, int]]) -> list[Finding]:
    """S1 and S2: a slot must stand alone, so code can never print a number nobody verified."""
    out: list[Finding] = []
    for i, (a, b) in enumerate(slots):
        before = text[a - 1] if a > 0 else ""
        after = text[b] if b < len(text) else ""
        nxt = text[b:].lstrip()[:1]
        word = _word_after(text, b)
        why = None
        if i + 1 < len(slots) and not any(unicodedata.category(c)[0] == "L" for c in text[b:slots[i + 1][0]]):
            why = "two slots with no word between them"
        elif not _allowed_beside_slot(before) and before not in DASHES:
            why = f"slot touches {before!r} before it"
        elif not _allowed_beside_slot(after):
            why = f"slot touches {after!r} after it"
        elif nxt and unicodedata.category(nxt)[0] == "S":
            why = f"symbol after a slot: {nxt!r}"
        elif word in MULTIPLIERS:
            why = f"multiplier or scale after a slot: {word!r}"
        else:
            why = _before(text, a)
        if why:
            out.append(Finding(FindingKind.SLOT_ADJACENT, a, b, text[a:b], why))
    return out


def _as_library(claims: Mapping[str, Claim] | Iterable[Claim], tenant_id: str) -> tuple[dict[str, Claim], set[str]]:
    """The code-keyed library and the ids of rows that are not the latest of their code. A mapping
    must be keyed by code (`repository.library`); a sequence of rows may hold several versions."""
    if isinstance(claims, Mapping):
        check_library(claims, tenant_id)
        rows = list(claims.values())
    else:
        rows = list(claims)
        for claim in rows:
            if claim.tenant_id != tenant_id:
                raise LibraryError(f"{claim.code}: claim belongs to another tenant")
    latest: dict[str, Claim] = {}
    for claim in rows:
        if claim.code not in latest or claim.version > latest[claim.code].version:
            latest[claim.code] = claim
    superseded = {c.supersedes_id for c in rows if c.supersedes_id}
    stale = {c.id for c in rows if c.version < latest[c.code].version or c.id in superseded}
    return latest, stale


def _claim_problem(claim: Claim, *, now, language: Language, stale: set[str],
                   allowed_claim_ids: Collection[str] | None, tenant_currency: str | None
                   ) -> tuple[FindingKind, str] | None:
    if claim.id in stale:
        return FindingKind.STALE_CLAIM, f"{claim.code} v{claim.version} is not the latest version"
    why = problem(claim, now)
    if why:
        return FindingKind.UNUSABLE_CLAIM, why
    if allowed_claim_ids is not None and claim.id not in allowed_claim_ids:
        return FindingKind.CLAIM_NOT_ALLOWED, f"{claim.code} is not allowed by this brief"
    wording = (claim.languages.get(language.value) or "").strip()
    if claim.kind is ClaimKind.NUMBER and claim.unit is None and not wording:
        return FindingKind.UNBOUND_NUMBER, (f"{claim.code} has no unit and no approved {language.value} wording, "
                                            "so nothing says what it counts")
    if (claim.kind is ClaimKind.PRICE and tenant_currency is not None and claim.unit != tenant_currency
            and (claim.unit or "") not in wording):
        return FindingKind.FOREIGN_CURRENCY, (f"{claim.code} is in {claim.unit}, not the tenant's {tenant_currency}, "
                                              "and its approved wording does not say so")
    return None


def compile_text(text: str, *, tenant_id: str, language: Language,
                 claims: Mapping[str, Claim] | Iterable[Claim], clock: Clock,
                 allowed_claim_ids: Collection[str] | None = None, protected_names: Iterable[str] = (),
                 allowed_terms: Iterable[str] = frozenset(), tenant_currency: str | None = None,
                 detectors: Sequence[Detector] = ()) -> CompileResult:
    """Check writer output before any slot is filled.

    `claims`: pass `library(repo, tenant_id)`, this tenant's latest versions keyed by code, so
    NAME claims protect their names even when unused. Rows of several versions may be passed as a
    sequence; any row older than another of its code is refused. A claim of another tenant, or
    under a key that is not its code, raises LibraryError.
    `allowed_terms`: human-approved tenant terms (product, place and street names) that never
    produce a lexical finding. The structural rules and protected names ignore them, and a term
    that overlaps a protected name raises AllowedTermError.
    `tenant_currency`: when given, a PRICE in another currency is refused unless its approved
    wording for the language names that currency."""
    library, stale = _as_library(claims, tenant_id)
    return _compile(text, tenant_id=tenant_id, language=language, library=library, stale=stale, clock=clock,
                    allowed_claim_ids=allowed_claim_ids, protected_names=protected_names,
                    allowed_terms=allowed_terms, tenant_currency=tenant_currency, detectors=detectors)


def _compile(text: str, *, tenant_id: str, language: Language, library: dict[str, Claim], stale: set[str],
             clock: Clock, allowed_claim_ids: Collection[str] | None, protected_names: Iterable[str],
             allowed_terms: Iterable[str], tenant_currency: str | None,
             detectors: Sequence[Detector]) -> CompileResult:
    names = protected_names_from(library.values(), protected_names)
    allowed_terms = list(allowed_terms)
    check_allowed_terms(allowed_terms, names)
    text = unicodedata.normalize("NFC", text)
    now = clock.now()
    if len(text) > MAX_TEXT_CHARS:
        return CompileResult(text=text, language=language, tenant_id=tenant_id, sentences=(), codes=(),
                             claim_ids=(), findings=tuple(scan(text, language=language)))

    findings: list[Finding] = []
    codes: list[str] = []
    slot_spans: list[tuple[int, int]] = []
    for m in SLOT.finditer(text):
        slot_spans.append(m.span())
        code = m.group(1)
        claim = library.get(code)
        a, b = m.span()
        if claim is None:
            findings.append(Finding(FindingKind.UNKNOWN_CLAIM, a, b, text[a:b], f"{code} is not in the truth library"))
        else:
            issue = _claim_problem(claim, now=now, language=language, stale=stale,
                                   allowed_claim_ids=allowed_claim_ids, tenant_currency=tenant_currency)
            if issue:
                findings.append(Finding(issue[0], a, b, text[a:b], issue[1]))
        if code not in codes:
            codes.append(code)

    findings += _slot_findings(text, slot_spans)
    findings += scan(text, language=language, marker_spans=slot_spans, protected_names=names,
                     allowed_terms=allowed_terms)

    sentences = _segment(text, language)  # display only: which sentence to show the writer
    if detectors:
        masked = _blank(text, slot_spans)
        for detector in detectors:
            findings.extend(detector.detect(masked, sentences, language))
    findings = [replace(f, sentence=sentence_at(sentences, f.start)) for f in findings]
    findings.sort(key=lambda f: (f.start, f.end, f.kind.value))
    claim_ids = tuple(library[c].id for c in codes if c in library)
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
        """For the human reviewer, to acknowledge one by one."""
        return self.compiled.flags


CHECK_NAME = "truth.compile"


def to_check_results(result: CompileResult, *, variant_id: str, content_sha256: str, clock: Clock,
                     attempt: int = 1, model_id: str | None = None) -> list[CheckResult]:
    """One `check_result` row (layer CODE) per BLOCK finding (verdict FAIL) and per FLAG finding
    (verdict FLAG, which the approver must acknowledge one by one), or a single PASS row.

    `content_sha256` is the hash of the variant content this text belongs to, from the gate's own
    function (`thallo.gate.variant_hash`, or `thallo.gate.content_hashes` from a repository). The
    gate counts a row only while the variant still hashes to it, so a FAIL here stops counting once
    the content is corrected, and counts again if the old content comes back."""
    rows = []
    for f in result.findings:
        verdict = CheckVerdict.FAIL if f.severity is Severity.BLOCK else CheckVerdict.FLAG
        rows.append(CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=result.tenant_id,
                                variant_id=variant_id, layer=CheckLayer.CODE, check_name=f"{CHECK_NAME}.{f.kind.value}",
                                verdict=verdict, reason=f"[{f.start}:{f.end}] {f.message}", attempt=attempt,
                                model_id=model_id, content_sha256=content_sha256))
    if not rows:
        rows.append(CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=result.tenant_id,
                                variant_id=variant_id, layer=CheckLayer.CODE, check_name=CHECK_NAME,
                                verdict=CheckVerdict.PASS, attempt=attempt, model_id=model_id,
                                content_sha256=content_sha256))
    return rows


def check_filled(filled: Filled) -> None:
    """S3: every digit in the output lies inside one rendered slot, and no enclosed or keycap
    digit is printed anywhere, approved wording included."""
    for a, b in _runs(filled.text, is_digit_char):
        if not any(x <= a and b <= y for x, y in filled.slot_spans):
            raise FillError([f"digits outside a slot after filling: {filled.text[a:b]!r}"])
    for a, b in _runs(filled.text, is_enclosed_digit):
        raise FillError([f"enclosed or keycap digit in the output: {filled.text[a:b]!r}"])


def build(text: str, *, tenant_id: str, language: Language, claims: Mapping[str, Claim] | Iterable[Claim],
          clock: Clock, default_language: Language, allowed_claim_ids: Collection[str] | None = None,
          protected_names: Iterable[str] = (), allowed_terms: Iterable[str] = frozenset(),
          tenant_currency: str | None = None, detectors: Sequence[Detector] = ()) -> Built:
    """Compile, fill, then check the output. The only public way to fill slots. Pass
    `library(repo, tenant_id)` as `claims`. Raises CompileFailed, FillError or LibraryError; never
    returns half-checked text. Review flags pass through on `Built.flags`."""
    library, stale = _as_library(claims, tenant_id)  # read once: compile and fill see the same rows
    result = _compile(text, tenant_id=tenant_id, language=language, library=library, stale=stale, clock=clock,
                      allowed_claim_ids=allowed_claim_ids, protected_names=protected_names,
                      allowed_terms=allowed_terms, tenant_currency=tenant_currency, detectors=detectors)
    if not result.ok:
        raise CompileFailed(result)
    filled = _fill(result.text, language=language, claims=library, clock=clock, default_language=default_language,
                   tenant_id=tenant_id)
    check_filled(filled)
    return Built(compiled=result, filled=filled)
