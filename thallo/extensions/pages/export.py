"""Export an approved page as files (the manual floor for a CMS connection).

Exporting is a send: it runs only on a `GateDecision` from `gate.check_publish` that allows this
exact variant, and renders from `decision.payload.text`, the text that was hashed and approved, so
nothing that was not approved can reach a file. A CMS publisher connection is not built here; until
one exists a person uploads these files.

Files: `<name>.html` (the article with its JSON-LD), `<name>.jsonld`, and `<name>.sitemap.xml`
(one `<url>` entry with `lastmod` set to the last verified date).
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from thallo.contracts.enums import Channel
from thallo.contracts.models import Claim
from thallo.extensions.pages.draft import last_verified_of
from thallo.extensions.pages.markup import INTERNAL_PATH, Cited, build_jsonld, parse, render_html, validate_jsonld
from thallo.gate.decision import GateDecision


class ExportRefused(Exception):
    pass


def export_files(decision: GateDecision, *, out_dir: Path, slug: str, base_url: str, claims: Sequence[Claim],
                 sources: Sequence[Cited]) -> list[Path]:
    if not isinstance(decision, GateDecision) or not decision.allowed or decision.payload is None:
        raise ExportRefused("the gate did not allow this page")
    payload = decision.payload
    if payload.variant_id != decision.variant_id or payload.channel != Channel.WEBSITE.value:
        raise ExportRefused("the approved payload is not this website page")
    if not INTERNAL_PATH.match(slug) or not base_url.startswith("https://"):
        raise ExportRefused("the slug or base address is not valid")
    page = parse(payload.text)
    verified: date | None = last_verified_of(claims)
    url = base_url.rstrip("/") + slug
    doc = build_jsonld(page, language=payload.language, url=url, last_verified=verified, sources=sources)
    problems = validate_jsonld(doc, page, last_verified=verified)
    if problems:
        raise ExportRefused("; ".join(problems))
    out_dir.mkdir(parents=True, exist_ok=True)
    name = slug.strip("/").replace("/", "--")
    html_path, ld_path, map_path = (out_dir / f"{name}.html", out_dir / f"{name}.jsonld",
                                    out_dir / f"{name}.sitemap.xml")
    html_path.write_text(render_html(page, language=payload.language, last_verified=verified, sources=sources,
                                     jsonld=doc), encoding="utf-8")
    ld_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    lastmod = f"<lastmod>{verified.isoformat()}</lastmod>" if verified else ""
    map_path.write_text(f"<url><loc>{escape(url)}</loc>{lastmod}</url>\n", encoding="utf-8")
    return [html_path, ld_path, map_path]
