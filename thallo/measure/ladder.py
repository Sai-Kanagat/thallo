"""The outcome ladder per piece, and three source signals side by side (BUILD_PLAN 2.6).

A piece is a variant that went live at least MATURE_DAYS before now. Rungs, highest first:

1. won: deals with a recorded `won` outcome. Revenue is the sum of `outcome.amount` only, never
   `deal.value` and never an estimate; a won outcome with no amount is counted in
   `won_amount_missing`, not as zero.
2. booked: leads with a booking that was not cancelled.
3. qualified: leads routed hot or warm by the fit rules.
4. high_intent: enquiry touches on the piece itself (chat code, form, call, walk-in code), the
   same kinds the planner forecasts as "enquiries".
5. engaged: saves, shares and comments from the latest metric snapshot. A metric the source did
   not return is missing (None), never zero. Reach and likes are never read.

Leads join a piece two ways, shown side by side and never blended: by the lead's first touch and
by its last touch. The third signal, the lead's own answer to "how did you hear about us", names a
channel, not a piece, so it is reported per source only and marked unavailable per piece.

Each piece also carries its review record, so outcome sits beside approval: approved on first
pass (a live approval and no edit on the variant), or edited first.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from thallo.contracts.enums import ApprovalSubject, LeadRoute
from thallo.contracts.models import (
    Ad,
    Approval,
    Asset,
    Booking,
    Brief,
    Bundle,
    Campaign,
    Deal,
    Edit,
    Idea,
    Lead,
    MetricSnapshot,
    Outcome,
    Pillar,
    Publication,
    Touch,
    Variant,
)
from thallo.contracts.states import BookingState
from thallo.planner.forecast import METRIC_KINDS
from thallo.planner.records import PlannerSlot
from thallo.tracking.report import source_share
from thallo.tracking.selfreport import normalise_answer

MATURE_DAYS = 14
RUNGS = ("won", "booked", "qualified", "high_intent", "engaged")
LEAD_RUNGS = ("won", "booked", "qualified")
VIEWS = ("first_touch", "last_touch")
ENGAGED_METRICS = ("saves", "shares", "comments")
QUALIFIED_ROUTES = frozenset({LeadRoute.HOT, LeadRoute.WARM})
HIGH_INTENT = METRIC_KINDS["enquiries"]


@dataclass
class PieceRow:
    variant_id: str
    channel: str
    pillar_id: str | None
    pillar: str | None
    format: str | None
    prompt_version: str | None
    sent_at: datetime
    first_pass_approved: bool
    edits: int
    rungs: dict[str, dict[str, int | None]] = field(default_factory=dict)  # view -> rung -> count
    won_amount: dict[str, str] = field(default_factory=dict)  # view -> recorded total
    won_amount_missing: dict[str, int] = field(default_factory=dict)  # view -> won deals with no amount

    def count(self, rung: str, view: str) -> int | None:
        return self.rungs[view][rung]

    def as_dict(self) -> dict[str, Any]:
        return {"variant_id": self.variant_id, "channel": self.channel, "pillar": self.pillar,
                "format": self.format, "sent_at": self.sent_at.isoformat(),
                "review": {"first_pass_approved": self.first_pass_approved, "edits": self.edits},
                "outcomes": self.rungs, "won_amount": self.won_amount,
                "won_amount_missing": self.won_amount_missing, "self_report": "unavailable per piece"}


@dataclass(frozen=True)
class LeadFacts:
    qualified: bool
    booked: bool
    won: bool
    amount: Decimal | None


def _lead_facts(tx: Any) -> dict[str, LeadFacts]:
    bookings = {b.lead_id for b in tx.list_all(Booking) if b.state is not BookingState.CANCELLED}
    deals = {d.id: d.lead_id for d in tx.list_all(Deal)}
    won: dict[str, list[Decimal | None]] = defaultdict(list)
    for o in tx.list_all(Outcome):
        if o.kind.value == "won" and o.deal_id in deals:
            won[deals[o.deal_id]].append(o.amount)
    out = {}
    for lead in tx.list_all(Lead):
        amounts = won.get(lead.id, [])
        known = [a for a in amounts if a is not None]
        out[lead.id] = LeadFacts(lead.route in QUALIFIED_ROUTES, lead.id in bookings, bool(amounts),
                                 sum(known, Decimal(0)) if amounts and len(known) == len(amounts) else None)
    return out


def _engaged(snaps: list[MetricSnapshot]) -> int | None:
    if not snaps:
        return None
    latest = max(snaps, key=lambda s: (int(s.day), s.pulled_at))
    if any(m in latest.missing or m not in latest.metrics for m in ENGAGED_METRICS):
        return None
    return int(sum(latest.metrics[m] for m in ENGAGED_METRICS))


def pieces(tx: Any, now: datetime) -> list[PieceRow]:  # noqa: C901, PLR0914
    """Every mature piece, oldest first, with its ladder under both views."""
    cutoff = now - timedelta(days=MATURE_DAYS)
    pubs = {p.variant_id: p for p in tx.list_all(Publication) if p.sent_at is not None and p.sent_at <= cutoff}
    if not pubs:
        return []
    variants = {v.id: v for v in tx.list_all(Variant) if v.id in pubs}
    bundles = {b.id: b for b in tx.list_all(Bundle)}
    assets = {a.id: a for a in tx.list_all(Asset)}
    briefs = {b.id: b for b in tx.list_all(Brief)}
    ideas = {i.id: i for i in tx.list_all(Idea)}
    pillars = {p.id: p for p in tx.list_all(Pillar)}
    formats = {s.brief_id: s.format for s in tx.list_all(PlannerSlot)}
    approved = {a.subject_id for a in tx.list_all(Approval)
                if a.subject_kind is ApprovalSubject.BUNDLE and a.voided_at is None}
    edits = Counter(e.variant_id for e in tx.list_all(Edit))
    touches = {t.id: t for t in tx.list_all(Touch)}
    snaps: dict[str, list[MetricSnapshot]] = defaultdict(list)
    for s in tx.list_all(MetricSnapshot):
        snaps[s.publication_id].append(s)
    facts = _lead_facts(tx)
    by_view: dict[str, dict[str, list[str]]] = {v: defaultdict(list) for v in VIEWS}
    for lead in tx.list_all(Lead):
        for view, tid in (("first_touch", lead.first_touch_id), ("last_touch", lead.last_touch_id)):
            t = touches.get(tid) if tid else None
            if t is not None and t.variant_id:
                by_view[view][t.variant_id].append(lead.id)
    intent = Counter(t.variant_id for t in touches.values() if t.variant_id and t.kind in HIGH_INTENT)
    rows = []
    for vid, v in variants.items():
        bundle = bundles.get(v.bundle_id)
        asset = assets.get(bundle.asset_id) if bundle else None
        brief = briefs.get(asset.brief_id) if asset else None
        idea = ideas.get(brief.idea_id) if brief else None
        pillar = pillars.get(idea.pillar_id) if idea and idea.pillar_id else None
        row = PieceRow(vid, v.channel.value, pillar.id if pillar else None, pillar.name if pillar else None,
                       formats.get(brief.id) if brief else None, idea.prompt_version if idea else None,
                       pubs[vid].sent_at, bool(bundle and bundle.id in approved and not edits[vid]), edits[vid])
        engaged = _engaged(snaps.get(pubs[vid].id, []))
        for view in VIEWS:
            leads = [facts[i] for i in by_view[view].get(vid, []) if i in facts]
            won = [f for f in leads if f.won]
            row.rungs[view] = {"won": len(won), "booked": sum(f.booked for f in leads),
                               "qualified": sum(f.qualified for f in leads), "high_intent": intent[vid],
                               "engaged": engaged}
            row.won_amount[view] = str(sum((f.amount for f in won if f.amount is not None), Decimal(0)))
            row.won_amount_missing[view] = sum(1 for f in won if f.amount is None)
        rows.append(row)
    return sorted(rows, key=lambda r: (r.sent_at, r.variant_id))


# ---------------------------------------------------------------- sources side by side

def _touch_source(t: Touch, variants: dict[str, str], ads: dict[str, str]) -> str | None:
    if t.variant_id and t.variant_id in variants:
        return variants[t.variant_id]
    if t.ad_id and t.ad_id in ads:
        return ads[t.ad_id]
    return None


def sources(tx: Any, *, since: datetime, until: datetime) -> dict[str, Any]:
    """Leads per source under first touch, last touch and self-report, plus the unsourced share.
    None of the three is called the truth; each column is a plain count."""
    variants = {v.id: v.channel.value for v in tx.list_all(Variant)}
    campaigns = {c.id: f"ads:{c.platform}" for c in tx.list_all(Campaign)}
    ads = {a.id: campaigns.get(a.campaign_id, "ads") for a in tx.list_all(Ad)}
    all_touches = tx.list_all(Touch)
    touches = {t.id: t for t in all_touches}
    reports: dict[str, list[Touch]] = defaultdict(list)
    for t in all_touches:
        if t.person_id and t.self_report:
            reports[t.person_id].append(t)
    leads = [lead for lead in tx.list_all(Lead) if since <= lead.created_at < until]
    table: dict[str, dict[str, int]] = defaultdict(lambda: {"first_touch": 0, "last_touch": 0, "self_report": 0})
    for lead in leads:
        for view, tid in (("first_touch", lead.first_touch_id), ("last_touch", lead.last_touch_id)):
            t = touches.get(tid) if tid else None
            src = _touch_source(t, variants, ads) if t else None
            table[src or "none"][view] += 1
        answers = sorted(reports.get(lead.person_id, []), key=lambda t: t.occurred_at)
        key = normalise_answer(answers[-1].self_report) if answers else None
        table[(f"said:{key}" if key else "said:unrecognised") if answers else "none"]["self_report"] += 1
    share = source_share(leads, all_touches, since=since, until=until)
    return {"leads": len(leads), "unsourced": share.unsourced, "unsourced_share": share.unsourced_share,
            "by_source": [{"source": k, **v} for k, v in sorted(table.items())],
            "note": "Three signals side by side. None is the truth; the columns are not added up."}


__all__ = ["LEAD_RUNGS", "MATURE_DAYS", "RUNGS", "VIEWS", "PieceRow", "pieces", "sources"]
