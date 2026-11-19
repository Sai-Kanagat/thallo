"""Code checks on an answer-first page. Pattern-level ideas from AEO and GEO page checkers and from
sitemap and JSON-LD validators, ported as plain rules (QUALITY_BAR section 10, intake level 1).

Each check returns (name, verdict, reason). Names start with `pages.`; any FAIL blocks approval
(the gate counts a FAIL under any name), a FLAG must be acknowledged by the approver.

    pages.answer_first     the first block after the title is the answer, short, on the question
    pages.citable_facts    the page uses at least one verified claim and every claim has a source
    pages.last_verified    a last verified date can be derived from the claims used
    pages.table            at least one table with a header and two rows
    pages.jsonld           the JSON-LD parses, has the required properties and matches the page
    pages.internal_links   enough internal links, every one to a known page, no outside link
    pages.readability      sentence length by word count (language neutral, no English formula)
    pages.sameness         not a near copy of another page draft of this tenant
"""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from thallo.checks.text import similarity
from thallo.contracts.enums import CheckVerdict
from thallo.contracts.models import Claim
from thallo.extensions.pages.markup import INTERNAL_PATH, Page, validate_jsonld
from thallo.extensions.pages.queries import words

PASS, FLAG, FAIL = CheckVerdict.PASS, CheckVerdict.FLAG, CheckVerdict.FAIL
ANSWER_WORDS = (12, 70)
MIN_LINKS = 2
AVG_SENTENCE_WORDS = 24
LONG_SENTENCE_WORDS = 38
SAMENESS_LIMIT = 0.6
PREAMBLE = re.compile(
    r"^(in this (article|guide|post)|great question|have you ever|are you wondering|welcome|let'?s|"
    r"in questo (articolo|post)|ti sei mai chiesto|benvenut)", re.I)

Row = tuple[str, CheckVerdict, str | None]


@dataclass(frozen=True)
class PageFacts:
    claims_used: Sequence[Claim]
    sources: Mapping[str, Any]  # source id -> Source row, for the claims used
    last_verified: date | None


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def _count(text: str) -> int:
    return len(re.findall(r"[^\W_]+", text))


def answer_first(page: Page, question_words: frozenset[str]) -> Row:
    name = "pages.answer_first"
    if not page.title:
        return name, FAIL, "the page has no title line"
    if not page.answer or page.answer_index != 0:
        return name, FAIL, "the first block after the title is not the answer"
    n = _count(page.answer)
    if not ANSWER_WORDS[0] <= n <= ANSWER_WORDS[1]:
        return name, FAIL, f"the answer has {n} words; it needs {ANSWER_WORDS[0]} to {ANSWER_WORDS[1]}"
    if PREAMBLE.match(page.answer):
        return name, FAIL, "the answer opens with a preamble instead of the answer"
    asked = words(page.title) | question_words
    if asked and not (words(page.answer) & asked):
        return name, FAIL, "the answer shares no word with the question"
    return name, PASS, None


def citable_facts(facts: PageFacts) -> Row:
    name = "pages.citable_facts"
    if not facts.claims_used:
        return name, FAIL, "the page uses no verified claim, so nothing on it is citable"
    missing = [c.code for c in facts.claims_used if not c.source_id or c.source_id not in facts.sources]
    if missing:
        return name, FAIL, f"claims with no source on record: {', '.join(sorted(missing))}"
    return name, PASS, None


def last_verified(facts: PageFacts) -> Row:
    name = "pages.last_verified"
    if facts.last_verified is None:
        return name, FAIL, "no claim on the page carries a verification date"
    return name, PASS, None


def table(page: Page) -> Row:
    name = "pages.table"
    good = [t for t in page.tables if len(t) >= 3 and len(t[0]) >= 2 and all(len(r) == len(t[0]) for r in t)]
    if not good:
        return name, FAIL, "the page needs a table with a header row and at least two rows of equal width"
    return name, PASS, None


def jsonld(doc: Any, page: Page, facts: PageFacts) -> Row:
    problems = validate_jsonld(doc, page, last_verified=facts.last_verified)
    return ("pages.jsonld", FAIL, "; ".join(problems)) if problems else ("pages.jsonld", PASS, None)


def internal_links(page: Page, known_paths: Collection[str], *, own_path: str) -> Row:
    name = "pages.internal_links"
    bad = [t for _, t in page.links if not INTERNAL_PATH.match(t)]
    if bad:
        return name, FAIL, f"links that are not internal paths: {len(bad)}"
    unknown = sorted({t for _, t in page.links if t not in known_paths})
    if unknown:
        return name, FAIL, f"links to pages the site does not have: {', '.join(unknown[:5])}"
    targets = {t for _, t in page.links if t != own_path}
    if len(targets) < MIN_LINKS:
        return name, FAIL, f"{len(targets)} internal link(s); the page needs at least {MIN_LINKS}"
    return name, PASS, None


def readability(page: Page) -> Row:
    name = "pages.readability"
    sentences = [s for p in page.prose() for s in _sentences(p)]
    if not sentences:
        return name, FAIL, "the page has no reading text"
    counts = [_count(s) for s in sentences]
    avg = sum(counts) / len(counts)
    if avg > AVG_SENTENCE_WORDS:
        return name, FAIL, f"sentences average {avg:.0f} words; keep them under {AVG_SENTENCE_WORDS}"
    long = sum(1 for n in counts if n > LONG_SENTENCE_WORDS)
    if long:
        return name, FLAG, f"{long} sentence(s) over {LONG_SENTENCE_WORDS} words"
    return name, PASS, None


def sameness(text: str, others: Sequence[str]) -> Row:
    name = "pages.sameness"
    top = max((similarity(text, o) for o in others), default=0.0)
    if top >= SAMENESS_LIMIT:
        return name, FAIL, f"too close to another page draft (similarity {top:.2f})"
    return name, PASS, None


def run_all(page: Page, *, doc: Any, facts: PageFacts, question_words: frozenset[str],
            known_paths: Collection[str], own_path: str, text: str, other_drafts: Sequence[str]) -> list[Row]:
    return [answer_first(page, question_words), citable_facts(facts), last_verified(facts), table(page),
            jsonld(doc, page, facts), internal_links(page, known_paths, own_path=own_path), readability(page),
            sameness(text, other_drafts)]
