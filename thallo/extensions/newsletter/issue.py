"""The fortnightly issue (WP-29, BUILD_PLAN module 31): built from the pieces that beat their forecast.

    picks = winners(tx, period_end=today)
    outcome = draft_issue(registry=..., tenant=..., picks=picks, period_end=today, ...)
    with db.tenant(tenant.id) as repo:
        write_issue(repo, outcome)

Selection is plain code. A piece "beat its forecast" when its scored actual is above the sealed
forecast's upper bound (`ForecastScore.actual > Forecast.high`), scored inside the fortnight that
ends on `period_end`, and it has a live publication. A missing actual never counts. Pieces already
used in an earlier issue are skipped. No winners: no issue and no call, nothing is invented.

The issue is written by copy production on channel `newsletter` from the approved text of the
winning pieces (as untrusted source material, slots intact) and may use only the claims those
pieces used. It waits in CHECKING for the check stack and a named human at the gate. One issue per
tenant per period (unique in migration 0040). Performance numbers are never put into the copy.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from thallo.connections.registry import ConnectionRegistry
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Channel, FunnelStage, Language
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Asset,
    Brief,
    Bundle,
    Claim,
    Forecast,
    ForecastScore,
    Idea,
    Publication,
    Tenant,
    Variant,
    VariantClaim,
)
from thallo.copy.produce import CopyOutcome, CopyStatus, produce_copy
from thallo.extensions.newsletter.records import NewsletterIssue
from thallo.gate.canonical import LinkResolver

PROMPT_VERSION = "newsletter.issue/1"
TASK_TYPE = "newsletter_issue"
PERIOD = timedelta(days=14)
MAX_PIECES = 5


@dataclass(frozen=True)
class Pick:
    brief_id: str
    variant: Variant  # the published variant of the piece
    claim_ids: tuple[str, ...]
    margin: float  # actual minus the forecast's upper bound, for ordering only (never in copy)


def _end(d: date) -> datetime:
    return datetime.combine(d + timedelta(days=1), time.min, tzinfo=UTC)


def used_briefs(tx: Any) -> set[str]:
    return {b for i in tx.list_all(NewsletterIssue) for b in i.piece_brief_ids}


def winners(tx: Any, *, period_end: date, limit: int = MAX_PIECES) -> list[Pick]:
    end = _end(period_end)
    start = end - PERIOD
    forecasts = {f.id: f for f in tx.list_all(Forecast)}
    variants = {v.id: v for v in tx.list_all(Variant)}
    bundles = {b.id: b for b in tx.list_all(Bundle)}
    assets = {a.id: a for a in tx.list_all(Asset)}
    published: dict[str, Variant] = {}
    for p in sorted(tx.list_all(Publication), key=lambda p: (p.created_at, p.id)):
        v = variants.get(p.variant_id)
        b = bundles.get(v.bundle_id) if v else None
        a = assets.get(b.asset_id) if b else None
        if v is not None and a is not None and p.sent_at is not None:
            published.setdefault(a.brief_id, v)
    links: dict[str, list[str]] = {}
    for vc in tx.list_all(VariantClaim):
        links.setdefault(vc.variant_id, []).append(vc.claim_id)
    skip = used_briefs(tx)
    out: list[Pick] = []
    for s in tx.list_all(ForecastScore):
        f = forecasts.get(s.forecast_id)
        if f is None or s.actual is None or not (start <= s.scored_at < end) or s.actual <= f.high:
            continue
        if f.brief_id in skip or f.brief_id not in published:
            continue
        v = published[f.brief_id]
        out.append(Pick(f.brief_id, v, tuple(sorted(links.get(v.id, []))), s.actual - f.high))
    out.sort(key=lambda p: (-p.margin, p.brief_id))
    return out[:limit]


@dataclass(frozen=True)
class IssueOutcome:
    status: CopyStatus | str
    note: str
    copy: CopyOutcome | None = None
    idea: Idea | None = None
    brief: Brief | None = None
    issue: NewsletterIssue | None = None

    @property
    def variant(self) -> Variant | None:
        return self.copy.variant if self.copy else None

    def rows(self) -> list[Any]:
        made = self.copy.rows() if self.copy else []
        head = [r for r in (self.idea, self.brief) if r is not None] if made else []
        return [*head, *made, *([self.issue] if self.issue else [])]


NO_WINNERS = "no_winners"


def draft_issue(*, registry: ConnectionRegistry, tenant: Tenant, picks: Sequence[Pick], period_end: date,
                claims: Mapping[str, Claim], clock: Clock, resolver: LinkResolver, non_links: Collection[str],
                language: Language | None = None, rule_version: int = 1) -> IssueOutcome:
    if not picks:
        return IssueOutcome(NO_WINNERS, "No piece beat its forecast this fortnight, so there is no issue.")
    if any(p.variant.tenant_id != tenant.id for p in picks):
        raise ValueError("a piece belongs to another tenant")
    now = clock.now()
    lang = language or tenant.default_language
    allowed = sorted({c for p in picks for c in p.claim_ids})
    idea = Idea(id=new_id(clock), created_at=now, tenant_id=tenant.id, stage=FunnelStage.CONSIDERATION,
                title=f"Newsletter for the fortnight to {period_end.isoformat()}",
                rationale=f"{len(picks)} piece(s) did better than their sealed forecast.",
                prompt_version=PROMPT_VERSION)
    brief = Brief(id=new_id(clock), created_at=now, tenant_id=tenant.id, idea_id=idea.id,
                  audience="People who asked to receive the newsletter",
                  job_to_be_done="Catch up on the best of the last two weeks",
                  message="Retell the pieces in the source material, one short section each.",
                  channels=[Channel.NEWSLETTER], allowed_claim_ids=allowed, call_to_action="Reply with a question",
                  language=lang, prompt_version=PROMPT_VERSION)
    sources = [p.variant.text for p in picks]
    copy = produce_copy(registry=registry, tenant=tenant, brief=brief, claims=claims, sources=sources,
                        channel=Channel.NEWSLETTER, rule_version=rule_version, clock=clock, length="long",
                        task_type=TASK_TYPE, resolver=resolver, non_links=non_links)
    issue = None
    if copy.variant is not None:
        issue = NewsletterIssue(id=new_id(clock), created_at=clock.now(), tenant_id=tenant.id,
                                period_start=period_end - PERIOD + timedelta(days=1), period_end=period_end,
                                variant_id=copy.variant.id, piece_brief_ids=[p.brief_id for p in picks])
    return IssueOutcome(copy.status, copy.note, copy, idea, brief, issue)


def write_issue(repo: Any, outcome: IssueOutcome) -> None:
    conn = getattr(repo, "conn", None)
    if conn is None or not callable(getattr(conn, "transaction", None)):
        raise ValueError("write_issue needs a repo with a transaction")
    rows = outcome.rows()
    if any(str(r.tenant_id).lower() != str(repo.tenant_id).lower() for r in rows):
        raise ValueError("outcome rows belong to another tenant than the repo's")
    with conn.transaction():
        for row in rows:
            repo.insert(row)
