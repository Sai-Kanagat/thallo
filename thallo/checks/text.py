"""Code checks on the outgoing text (layer CODE, no model): slop, sameness, length per channel, and
the PII and prompt-injection screen. Each returns `Finding`s; the stack turns them into rows.

Every check reports under its one name on every run: one row per finding, or one PASS when it
found nothing, so a later attempt decides the key on its own (gate/checks.py).
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from thallo.contracts.enums import Channel, CheckVerdict, Language
from thallo.copy.slop import find_slop
from thallo.publish.preflight import channel_problems

SLOP = "copy.slop"
SAMENESS = "copy.sameness"
LENGTH = "copy.length"
PII = "copy.pii"
INJECTION = "copy.injection"


@dataclass(frozen=True)
class Finding:
    check: str
    verdict: CheckVerdict
    reason: str | None = None
    media_sha256: str | None = None


def _pass(check: str, media_sha256: str | None = None) -> Finding:
    return Finding(check, CheckVerdict.PASS, None, media_sha256)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace(chr(0x2019), "'")


# ---------------------------------------------------------------- slop

# Added to the copy slop list (thallo/copy/slop.py). Seeded by idea, not copied text, from public
# lists of tells in machine-written prose: Wikipedia's "Signs of AI writing" (WikiProject AI
# Cleanup) and the Humanizer checklist named in docs/QUALITY_BAR.md section 10. Patterns are our
# own wording; tune them from real reviewer edits.
_DASHES = "[" + chr(0x2013) + chr(0x2014) + "]"  # en and em dash, a habit of generated prose


def _words(*phrases: str) -> str:
    return r"\b(?:" + "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in phrases) + r")\b"


_EXTRA_EN: dict[str, list[str]] = {
    "significance_inflation": [_words(
        "stands as a testament", "a testament to", "plays a pivotal role", "pivotal moment",
        "underscores the importance",
        "serves as a reminder", "leaves a lasting impression", "marks a significant", "cannot be overstated")],
    "promotional_filler": [_words(
        "nestled in", "nestled within", "vibrant community", "rich tapestry", "breathtaking", "must-visit",
        "boasts a", "curated selection", "meticulously crafted", "elevate your", "embark on", "journey of")],
    "vague_authority": [_words("experts say", "studies show", "research shows", "many believe", "it is widely known")],
    "chat_residue": [_words("great question", "i hope this helps", "let me know if", "as an ai", "certainly!",
                            "in conclusion", "it's worth noting", "it is worth noting")],
    "whether_construction": [r"\bwhether\s+you'?re\s+[^.!?]{1,40}\bor\b"],
    "stacked_connectives": [r"(?:^|[.!?]\s+)(?:moreover|furthermore|additionally),"],
    "dash_habit": [_DASHES],
}
_EXTRA_IT: dict[str, list[str]] = {
    "significance_inflation": [_words("una testimonianza di", "un ruolo fondamentale", "sottolinea l'importanza")],
    "promotional_filler": [_words("immerso nel", "nel cuore di", "un viaggio", "mozzafiato", "selezione curata")],
    "chat_residue": [_words("in conclusione", "vale la pena notare", "spero che questo")],
    "dash_habit": [_DASHES],
}
EXTRA_SLOP: dict[Language, dict[str, list[re.Pattern[str]]]] = {
    lang: {k: [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in ps] for k, ps in table.items()}
    for lang, table in ((Language.EN, _EXTRA_EN), (Language.IT, _EXTRA_IT), (Language.TA, {}))
}


def slop(text: str, language: Language, extra_phrases: Iterable[str] = ()) -> list[Finding]:
    """FLAG per hit (a reviewer decides), PASS when clean. Slop never FAILs: it is taste, not fact."""
    folded = _fold(text)
    hits = [(h.start, f"{h.kind.value}: {h.match!r}") for h in find_slop(text, language, extra_phrases)]
    for kind, patterns in EXTRA_SLOP[language].items():
        hits += [(m.start(), f"{kind}: {m.group()!r}") for p in patterns for m in p.finditer(folded)]
    hits.sort()
    return [Finding(SLOP, CheckVerdict.FLAG, why) for _, why in hits] or [_pass(SLOP)]


# ---------------------------------------------------------------- sameness

SAMENESS_LIMIT = 0.6  # share of word 3-grams in common (Jaccard); at or above, redraft
RECENT_PIECES = 50
_SLOT = re.compile(r"\{\{\s*C-\d+\s*\}\}")
_WORD = re.compile(r"\w+", re.UNICODE)

# A small corpus of generic AI marketing copy, written for this package (not collected from anyone).
GENERIC_CORPUS: tuple[str, ...] = (
    "In today's fast-paced world, finding the perfect solution can be a challenge. That's where we come in.",
    "We are passionate about delivering exceptional quality and unparalleled service to every customer.",
    "Discover the difference that quality makes. Our team is dedicated to exceeding your expectations.",
    "Elevate your experience with our curated selection of premium products, crafted with care.",
    "Whether you're looking for something special or an everyday essential, we have something for everyone.",
    "Join our community today and unlock a world of possibilities. Your journey starts here.",
    "Experience the perfect blend of tradition and innovation, designed with you in mind.",
    "Don't miss out on this amazing opportunity. Visit us today and see what makes us different.",
)


def _shingles(text: str, n: int = 3) -> frozenset[tuple[str, ...]]:
    words = [w.casefold() for w in _WORD.findall(_SLOT.sub(" ", _fold(text)))]
    if len(words) < n:
        return frozenset({tuple(words)}) if words else frozenset()
    return frozenset(tuple(words[i:i + n]) for i in range(len(words) - n + 1))


def similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def sameness(text: str, recent: Sequence[str], *, limit: float = SAMENESS_LIMIT,
             corpus: Sequence[str] = GENERIC_CORPUS) -> list[Finding]:
    """FAIL when the text is too close to one of the brand's last `RECENT_PIECES` pieces or to the
    generic corpus (BUILD_PLAN 2.7: "too close to either and it is redrafted"); PASS otherwise."""
    out: list[Finding] = []
    for label, pool in (("a recent piece", list(recent)[-RECENT_PIECES:]), ("generic AI copy", list(corpus))):
        scored = [(similarity(text, other), i) for i, other in enumerate(pool)]
        if scored:
            best, i = max(scored)
            if best >= limit:
                out.append(Finding(SAMENESS, CheckVerdict.FAIL,
                                   f"{best:.0%} of its word sequences match {label} (#{i + 1}); limit {limit:.0%}"))
    return out or [_pass(SAMENESS)]


# ---------------------------------------------------------------- length

def length(text: str, channel: Channel, *, media_count: int = 0, max_chars: int | None = None) -> list[Finding]:
    """FAIL on anything the publisher's own preflight would refuse for this channel (its limits, one
    table: `publish.preflight.channel_problems`), and over the brief's own `max_chars`."""
    out = [Finding(LENGTH, CheckVerdict.FAIL, p) for p in channel_problems(channel.value, text, media_count)]
    if max_chars and len(text) > max_chars:
        out.append(Finding(LENGTH, CheckVerdict.FAIL, f"{len(text)} characters, over the brief's {max_chars}"))
    return out or [_pass(LENGTH)]


# ---------------------------------------------------------------- PII and injection screen

_EMAIL = re.compile(r"[\w.+%-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w])\+?\d[\d \t().-]{7,18}\d(?![\w])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_CODICE_FISCALE = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPRST]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)

_INJECTION = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|your)\s+"
    r"(?:instructions|prompts?|rules)",
    r"\bsystem\s+prompt\b",
    r"\byou\s+are\s+now\s+(?:an?\s+)?(?:ai|assistant|chatbot|language\s+model|dan|unrestricted|unfiltered)\b",
    r"\bdeveloper\s+mode\b",
    r"\bjailbreak",
    r"\bas\s+an\s+ai\s+(?:language\s+)?model\b",
    r"<\|?(?:im_start|im_end|system|endoftext)\|?>",
    r"\[/?INST\]",
    r"</?(?:system|assistant|instructions?)>",
    r"(?m)^\s*(?:system|assistant|developer)\s*:",
    r"\bignora\s+(?:le\s+)?istruzioni\s+precedenti\b",
)]


# Zero-width and bidi control characters can split or hide a phrase from a pattern: removed before
# the injection screen matches (the outgoing text itself is unchanged).
_HIDDEN = re.compile("[" + "".join(chr(c) for c in (*range(0x200B, 0x2010), *range(0x202A, 0x202F),
                                                     *range(0x2060, 0x2065), *range(0x2066, 0x206A), 0xFEFF,
                                                     0x00AD, 0x180E)) + "]")
SOURCE_INJECTION = "copy.source_injection"


def _injections(text: str) -> list[str]:
    plain = _HIDDEN.sub("", _fold(text))
    return [m.group() for p in _INJECTION for m in p.finditer(plain)]


def screen_sources(material: Sequence[str]) -> list[Finding]:
    """Instruction-shaped text in a source (a brief, a transcript, a comment) FLAGs: the source is
    untrusted data, so the approver should know it tried to steer the writer. PASS when clean."""
    out = [Finding(SOURCE_INJECTION, CheckVerdict.FLAG, f"a source contains instruction-shaped text: {hit!r}")
           for item in material for hit in _injections(item)]
    return out or [_pass(SOURCE_INJECTION)]


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1
    except ValueError:
        return False


def _mask(value: str) -> str:
    v = value.strip()
    return v[:2] + "*" * max(len(v) - 4, 1) + v[-2:] if len(v) > 4 else "*" * len(v)


def screen(text: str, *, allowed_contacts: Iterable[str] = ()) -> list[Finding]:
    """PII: a card number or IBAN that checks out, or an Italian tax code, FAILs (never in a post);
    an email address or phone number the brand has not listed as its own FLAGs. Injection: any
    instruction-shaped text aimed at a model FAILs, since it means a source steered the writer.
    Reasons mask the values."""
    folded = _fold(text)
    allowed_emails = {a.strip().casefold() for a in allowed_contacts}
    allowed = {re.sub(r"[\s().-]", "", a).casefold() for a in allowed_contacts}
    pii: list[Finding] = []
    for m in _CARD.finditer(folded):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            pii.append(Finding(PII, CheckVerdict.FAIL, f"a payment card number: {_mask(digits)}"))
    pii += [Finding(PII, CheckVerdict.FAIL, f"a bank account number (IBAN): {_mask(m.group())}")
            for m in _IBAN.finditer(folded) if _iban_ok(m.group())]
    pii += [Finding(PII, CheckVerdict.FAIL, f"a personal tax code: {_mask(m.group())}")
            for m in _CODICE_FISCALE.finditer(folded)]
    pii += [Finding(PII, CheckVerdict.FLAG, f"an email address the brand has not listed: {_mask(m.group())}")
            for m in _EMAIL.finditer(folded) if m.group().casefold() not in allowed_emails]
    for m in _PHONE.finditer(folded):
        digits = re.sub(r"[\s().-]", "", m.group())
        if sum(ch.isdigit() for ch in digits) >= 8 and digits.casefold() not in allowed and not any(
                f.reason and _mask(re.sub(r"\D", "", m.group())) in f.reason for f in pii):
            pii.append(Finding(PII, CheckVerdict.FLAG, f"a phone number the brand has not listed: {_mask(digits)}"))
    injection = [Finding(INJECTION, CheckVerdict.FAIL, f"instruction-shaped text aimed at a model: {hit!r}")
                 for hit in _injections(text)]
    return (pii or [_pass(PII)]) + (injection or [_pass(INJECTION)])


__all__ = [
    "EXTRA_SLOP", "GENERIC_CORPUS", "INJECTION", "LENGTH", "PII", "RECENT_PIECES", "SAMENESS",
    "SAMENESS_LIMIT", "SLOP", "SOURCE_INJECTION", "Finding", "length", "sameness", "screen", "screen_sources",
    "similarity", "slop",
]
