"""Sai Kanagat's demo, built from the files in `demo_brands/sai/`. Used by demo_seed.py for `--brand sai`.

Only facts his public site states (`claims.json`) and his own words (`voice.md`) go in. The six claims the
site contradicts itself on (`status: conflicting` in claims.json, listed in GAPS.md) are stored but stay
candidates, so no piece can use them until Sai says which version is right.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from demo_spec import ClaimSpec, Demo, Piece, Pillar, Slot, SourceSpec

from thallo.contracts.enums import Channel, ClaimKind, FunnelStage, SourceKind
from thallo.contracts.states import PlanSlotState

READ_ON = datetime(2026, 11, 16, tzinfo=UTC)  # the day the site was read (claims.json)
NUMBERS = {"C12": ("25", "%"), "C15": ("35", "%"), "C16": ("40", "%")}  # percentages: typed numbers, not sentences
LI = Channel.LINKEDIN_PERSONAL


def claim_specs(path: Path) -> tuple[tuple[SourceSpec, ...], tuple[ClaimSpec, ...]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    pages = sorted({c["page"] for c in data["claims"]})
    sources = tuple(SourceSpec(page, f"saikanagat.com{page}", SourceKind.WEB_PAGE, f"https://saikanagat.com{page}",
                               READ_ON) for page in pages)
    claims = []
    for c in data["claims"]:
        kind, value, unit = ((ClaimKind.NUMBER, *NUMBERS[c["id"]]) if c["id"] in NUMBERS
                             else (ClaimKind.CAPABILITY, c["claim"], None))
        claims.append(ClaimSpec(code=f"C-{int(c['id'][1:]):04d}", kind=kind, value=value, unit=unit, source=c["page"],
                                evidence="\n".join(c["fragments"]),
                                span={"page": c["page"], "text": " ... ".join(c["fragments"])},
                                verified=c["status"] == "stated"))
    return sources, tuple(claims)


def voice(path: Path) -> tuple[tuple[tuple[str, str, str], ...], tuple[str, ...]]:
    """The Do and Do not rules with the quote ids that back them, and the words his brand book retires."""
    text = path.read_text(encoding="utf-8")
    rules, section = [], ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = {"## Do": "do", "## Do not": "dont"}.get(line.strip(), "")
        elif section and (m := re.match(r"^\d+\. (.+?)(?: \((Q[\d, Q]+)\))?$", line)):
            rules.append((section, m.group(1), m.group(2) or ""))
    retired = re.search(r"^\| Q8 \|[^|]*\| \"([^\"]+)\" \|$", text, re.M)
    if not rules or retired is None:
        raise SystemExit("voice.md no longer has the Do, Do not and Q8 parts demo_sai.py reads")
    return tuple(rules), tuple(w.strip() for w in retired.group(1).split("·"))


THALLO_SOURCES = (
    "Thallo is an AI system that runs a brand's marketing end to end, an early build with a waitlist. "
    "(saikanagat.com/llms.txt)",
    "Nothing is decorative. (saikanagat.com/process.html)",
    "Design Things That Sell. (saikanagat.com/index.html)",
)
VIJAY_SOURCES = (
    "At Vijay Gas Service, marketing expenses were reduced by 25%. (saikanagat.com/about.html)",
    "A real number, before the adjective. (saikanagat.com/brand-identity.html)",
)
QUOTE_SOURCES = (
    "A real number, before the adjective. (saikanagat.com/brand-identity.html)",
    "Beauty is a means, never the brief. (saikanagat.com/brand-identity.html)",
)
SEO_SOURCES = (
    "+40% organic search traffic from a structured 6-month SEO content programme (keyword research, editorial "
    "planning, long-form production). (saikanagat.com/about.html)",
)
META_SOURCES = (
    "In the Meta Ads account he took over, 47 active ad variations across 12 campaigns were cut to 8 across 3 "
    "campaigns. (saikanagat.com/blog/what-15k-meta-ads-taught-me.html)",
)

PIECES = (
    # A LinkedIn post about one project. The first draft is the kind of opener the slop catalogue removes.
    Piece("thallo",
          ("Look no further: Thallo is a game changer. {{C-0021}}",
           "Thallo, in one line: {{C-0021}} Nothing is decorative. Design things that sell."),
          "single-01", {"headline": "Thallo", "body": "{{C-0021}}", "cta": "See the work"},
          "Paper card headed Thallo, with the line describing Thallo as an early build with a waitlist, and the "
          "Sai Kanagat mark",
          LI, allowed=("C-0021",), sources=THALLO_SOURCES),
    # A case-study card: the number is a typed claim, filled by code both in the post and on the card.
    Piece("vijay",
          ("A case I can put a number on. At Vijay Gas Service, marketing expenses came down by {{C-0012}}. "
           "A real number, before the adjective.",),
          "single-06", {"headline": "{{C-0012}} lower marketing expenses", "body": "At Vijay Gas Service."},
          "Paper card: 25% lower marketing expenses at Vijay Gas Service", LI, allowed=("C-0012",),
          sources=VIJAY_SOURCES),
    # A quote card made of his own words, nothing to fill.
    Piece("quote",
          ("A real number, before the adjective. Beauty is a means, never the brief.",),
          "quote-01", {"quote": "A real number, before the adjective.", "attribution": "Sai Kanagat, brand book"},
          "Paper card with the quote A real number, before the adjective.", LI, allowed=(), sources=QUOTE_SOURCES),
    # A stat card whose copy carries a flag: "only" asks the approver whether that is really so.
    Piece("seo",
          ("Organic search traffic grew by {{C-0016}}. The only change was a structured content programme: keyword "
           "research, editorial planning, long-form production.",),
          "proof-01", {"figure": "{{C-0016}}", "headline": "Organic search traffic, up",
                       "detail": "Keyword research, editorial planning, long-form production.",
                       "source": "saikanagat.com, about"},
          "Paper stat card: 40% growth in organic search traffic", LI, allowed=("C-0016",), sources=SEO_SOURCES),
    # A draft that types the numbers instead of using the claim: four tries, none compiles, a person gets it.
    Piece("meta",
          ("I took over a Meta Ads account with 47 live ad variations and cut it to 8.",
           "Took over a Meta Ads account: 47 ad variations across 12 campaigns, cut to 8 across 3.",
           "I cut 47 ad variations to 8.",
           "Forty-seven ad variations became eight."),
          "single-08", {"headline": "One Meta Ads account, cut back", "body": "{{C-0017}}"},
          "Paper card about the Meta Ads account clean-up", LI, candidates=1, allowed=("C-0017",),
          sources=META_SOURCES, blocked_draft=True),
)


def sai_demo(root: Path) -> Demo:
    sources, claims = claim_specs(root / "claims.json")
    rules, retired = voice(root / "voice.md")
    return Demo(
        key="sai", tenant_name="Sai Kanagat", slug="sai-kanagat", person="Sai Kanagat", sources=sources,
        claims=claims, pieces=PIECES,
        pillars=(Pillar("Made to last", FunnelStage.CONSIDERATION, "0.5"),
                 Pillar("Built to grow", FunnelStage.AWARENESS, "0.5")),
        plan=(Slot(0, LI, FunnelStage.CONSIDERATION, PlanSlotState.MISSED),
              Slot(1, LI, FunnelStage.CONSIDERATION, PlanSlotState.FILLED, "thallo"),
              Slot(2, LI, FunnelStage.AWARENESS, PlanSlotState.FILLED, "vijay", pillar=1),
              Slot(3, LI, FunnelStage.AWARENESS, PlanSlotState.OPEN, pillar=1),
              Slot(5, LI, FunnelStage.CONSIDERATION, PlanSlotState.OPEN)),
        default_sources=THALLO_SOURCES,
        audience="A hiring manager in Italy with seven minutes between meetings",
        job="Understand in one read what he builds and what it did",
        call_to_action="See the work", brand_dir=root, voice_rules=rules, extra_slop=retired)
