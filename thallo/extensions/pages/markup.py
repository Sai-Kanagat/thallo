"""The page format, one text that is both the variant the gate hashes and the page that is exported.

The writer produces slot-bearing text in a small block markup, so the truth compiler reads every
sentence and nothing outside the hashed text decides what the page says:

    # Title, the question in the reader's words
    > The direct answer, first, in one short paragraph.
    ## A section heading
    A paragraph.
    | Header ;; Header
    | Row label ;; cell with a slot {{C-0001}} and a word after it
    ? A question people also ask
    Its answer, on the next line.
    [anchor text](/an-internal-path)

Tables use ` ;; ` between cells because a slot may not touch a symbol such as a pipe. Links are
internal paths of letters and hyphens only: no host, no dot, no digit, so the gate sees no link to
resolve and the compiler sees no digit. HTML and JSON-LD are derived in code from the filled text
and the claims it used; the "last verified" date is the oldest verification among those claims, so
it never overstates how fresh the page is.
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

LINK = re.compile(r"\[([^\]\n]{1,120})\]\(([^)\s]{1,200})\)")
INTERNAL_PATH = re.compile(r"^/[a-z]+(-[a-z]+)*(/[a-z]+(-[a-z]+)*)*$")
CELL_SPLIT = " ;; "


@dataclass
class Section:
    heading: str
    paragraphs: list[str] = field(default_factory=list)


@dataclass
class Page:
    title: str = ""
    answer: str = ""
    answer_index: int = -1  # block index of the answer (0 is the first block after the title)
    sections: list[Section] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    faqs: list[tuple[str, str]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor, target)
    stray: list[str] = field(default_factory=list)  # lines before any block that are not the title

    def prose(self) -> list[str]:
        """Every paragraph-like piece of reading text, for readability."""
        out = [self.answer] if self.answer else []
        for s in self.sections:
            out += s.paragraphs
        out += [a for _, a in self.faqs]
        return [strip_links(p) for p in out if p.strip()]


def strip_links(text: str) -> str:
    return LINK.sub(lambda m: m.group(1), text)


def parse(text: str) -> Page:
    page = Page()
    blocks = 0
    table: list[list[str]] | None = None
    pending_q: str | None = None
    current: Section | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            table = None
            continue
        page.links += [(m.group(1), m.group(2)) for m in LINK.finditer(line)]
        if line.startswith("| "):
            if table is None:
                table = []
                page.tables.append(table)
                blocks += 1
            table.append([c.strip() for c in line[2:].split(CELL_SPLIT)])
            continue
        table = None
        if line.startswith("# ") and not page.title:
            page.title = line[2:].strip()
        elif line.startswith("> "):
            if not page.answer:
                page.answer, page.answer_index = line[2:].strip(), blocks
            else:
                page.answer += " " + line[2:].strip()
            blocks += 1
        elif line.startswith("## "):
            current = Section(line[3:].strip())
            page.sections.append(current)
            blocks += 1
        elif line.startswith("? "):
            pending_q = line[2:].strip()
            blocks += 1
        elif pending_q is not None:
            page.faqs.append((pending_q, line))
            pending_q = None
        elif current is not None:
            current.paragraphs.append(line)
        else:
            page.stray.append(line)
            blocks += 1
    return page


# ---------------------------------------------------------------- rendering

def _inline(text: str) -> str:
    out, pos = [], 0
    for m in LINK.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        target = m.group(2)
        href = html.escape(target, quote=True) if INTERNAL_PATH.match(target) else "#"
        out.append(f'<a href="{href}">{html.escape(m.group(1))}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


@dataclass(frozen=True)
class Cited:
    """A source behind a claim the page uses, for the visible source list and JSON-LD."""

    title: str
    uri: str | None


def render_html(page: Page, *, language: str, last_verified: date | None, sources: Sequence[Cited],
                jsonld: Mapping[str, Any]) -> str:
    parts = [f'<article lang="{html.escape(language)}">', f"<h1>{html.escape(page.title)}</h1>"]
    if page.answer:
        parts.append(f'<p class="answer">{_inline(page.answer)}</p>')
    for s in page.sections:
        parts.append(f"<h2>{html.escape(s.heading)}</h2>")
        parts += [f"<p>{_inline(p)}</p>" for p in s.paragraphs]
    for t in page.tables:
        head, *body = t
        parts.append("<table><thead><tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in head)
                     + "</tr></thead><tbody>")
        parts += ["<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body]
        parts.append("</tbody></table>")
    if page.faqs:
        parts.append("<section class=\"faq\">")
        for q, a in page.faqs:
            parts.append(f"<h3>{html.escape(q)}</h3><p>{_inline(a)}</p>")
        parts.append("</section>")
    if sources:
        parts.append("<section class=\"sources\"><h2>Sources</h2><ul>")
        for s in sources:
            if s.uri and s.uri.startswith("https://"):
                parts.append(f'<li><a href="{html.escape(s.uri, quote=True)}">{html.escape(s.title)}</a></li>')
            else:
                parts.append(f"<li>{html.escape(s.title)}</li>")
        parts.append("</ul></section>")
    if last_verified:
        iso = last_verified.isoformat()
        parts.append(f'<p class="verified">Last verified <time datetime="{iso}">{iso}</time></p>')
    body = json.dumps(jsonld, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    parts.append(f'<script type="application/ld+json">{body}</script>')
    parts.append("</article>")
    return "\n".join(parts)


def build_jsonld(page: Page, *, language: str, url: str, last_verified: date | None,
                 sources: Sequence[Cited]) -> dict[str, Any]:
    graph: list[dict[str, Any]] = [{
        "@type": "Article",
        "headline": page.title,
        "inLanguage": language,
        "mainEntityOfPage": url,
        "abstract": strip_links(page.answer),
        **({"dateModified": last_verified.isoformat()} if last_verified else {}),
        "citation": [{"@type": "CreativeWork", "name": s.title, **({"url": s.uri} if s.uri else {})}
                     for s in sources],
    }]
    if page.faqs:
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": strip_links(a)}}
            for q, a in page.faqs]})
    return {"@context": "https://schema.org", "@graph": graph}


def validate_jsonld(doc: Any, page: Page, *, last_verified: date | None) -> list[str]:
    """Pattern-level checks a JSON-LD validator makes, plus agreement with the visible page."""
    problems: list[str] = []
    try:
        doc = json.loads(json.dumps(doc))
    except (TypeError, ValueError):
        return ["the JSON-LD does not serialise"]
    if not isinstance(doc, dict) or doc.get("@context") not in ("https://schema.org", "http://schema.org"):
        return ["the JSON-LD has no schema.org @context"]
    graph = doc.get("@graph")
    if not isinstance(graph, list) or not graph:
        return ["the JSON-LD has no @graph"]
    by_type: dict[str, dict[str, Any]] = {}
    for node in graph:
        if not isinstance(node, dict) or not isinstance(node.get("@type"), str):
            problems.append("a JSON-LD node has no @type")
            continue
        by_type[node["@type"]] = node
    art = by_type.get("Article")
    if art is None:
        problems.append("the JSON-LD has no Article")
    else:
        if art.get("headline") != page.title or not page.title:
            problems.append("the Article headline does not match the visible title")
        if len(str(art.get("headline", ""))) > 110:
            problems.append("the Article headline is longer than 110 characters")
        if last_verified and art.get("dateModified") != last_verified.isoformat():
            problems.append("dateModified does not match the last verified date")
        if not art.get("citation"):
            problems.append("the Article cites no source")
    faq = by_type.get("FAQPage")
    if page.faqs:
        if faq is None:
            problems.append("the page has questions but the JSON-LD has no FAQPage")
        else:
            seen = [(q.get("name"), (q.get("acceptedAnswer") or {}).get("text"))
                    for q in faq.get("mainEntity") or [] if isinstance(q, dict)]
            if seen != [(q, strip_links(a)) for q, a in page.faqs]:
                problems.append("the FAQPage questions do not match the visible questions and answers")
    elif faq is not None:
        problems.append("the JSON-LD has an FAQPage but the page shows no questions")
    return problems
