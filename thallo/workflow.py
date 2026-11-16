"""The weekly state machine. Call tick() on a schedule; it is safe to repeat.

The original engine. It no longer sends anything: its old publish path (the legacy `Gate` and
`publishing.deliver`) was removed in WP-08, so the only way out is `thallo.publish`, which calls
`gate.check_publish` on approved bundle rows. When a scheduled item comes due, the engine tells
the cockpit once that it is due and leaves it SCHEDULED; a person marks it Published (with its
link) once it is live, and measurement then runs as before."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from thallo.analytics import MetricStore
from thallo.contracts import BrandSettings, Cockpit, Item, MetricsSource, Passport, Status, Writer, week_offsets
from thallo.foresight import ForecastLedger
from thallo.gate.legacy import content_hash
from thallo.taste import TasteLedger
from thallo.truth import ClaimLedger, render

LIMITS = {"linkedin": 3000, "instagram": 2200, "tiktok": 2200, "x": 280}
DUE = "Due now. The engine does not send: publish it through the publisher, or post it by hand and mark it Published"


def preflight(item: Item) -> list[str]:
    errors = []
    text = render(item.text)
    if not text.strip():
        errors.append("empty text")
    limit = LIMITS.get(item.channel)
    if limit and len(text) > limit:
        errors.append(f"over {limit} characters")
    if item.channel in ("instagram", "tiktok") and not item.media:
        errors.append("media required")
    return errors


@dataclass
class Engine:
    brand: BrandSettings
    cockpit: Cockpit
    writer: Writer
    metrics_source: MetricsSource
    claims: ClaimLedger
    taste: TasteLedger = field(default_factory=TasteLedger)
    forecasts: ForecastLedger = field(default_factory=ForecastLedger)
    metrics: MetricStore = field(default_factory=MetricStore)
    passports: dict[str, Passport] = field(default_factory=dict)
    drafts: dict[str, str] = field(default_factory=dict)
    announced: set[str] = field(default_factory=set)

    def tick(self, now: datetime) -> None:
        for item in self.cockpit.items():
            self._sync_approval(item, now)
            times = week_offsets(item.publish_at)
            if item.status is Status.IDEA and now >= times["generate"]:
                self._generate(item, now)
            elif item.status is Status.REVIEW and now >= times["hold"]:
                item.status = Status.ON_HOLD
                self.cockpit.notify(item, "Not approved in time, slot on hold")
            elif item.status is Status.APPROVED and now >= times["schedule"]:
                self._schedule(item, now)
            elif item.status is Status.SCHEDULED and now >= item.publish_at:
                self._due(item)
            elif item.status is Status.PUBLISHED:
                self._measure(item, now)
            self.cockpit.save(item)

    def _sync_approval(self, item: Item, now: datetime) -> None:
        """A person set Approved in the cockpit. Seal it, or send it back if it no longer holds."""
        if item.status not in (Status.APPROVED, Status.SCHEDULED):
            return
        passport = self.passports.get(item.id)
        if passport:
            current = content_hash(item, passport.rule_version, self.claims, passport.claim_ids)
            if current == passport.content_hash:
                return
            self.passports.pop(item.id)
            item.status = Status.REVIEW
            self.cockpit.notify(item, "Changed after approval, please review again")
            return
        if item.status is Status.SCHEDULED:
            item.status = Status.REVIEW
            self.cockpit.notify(item, "Scheduled without approval, please review")
            return
        try:
            self.approve(item, "cockpit", now)
        except ValueError as error:
            item.status = Status.REVIEW
            self.cockpit.notify(item, f"Approval refused: {error}")

    def _generate(self, item: Item, now: datetime) -> None:
        item.text = self.writer.draft(item, self.taste.rules)
        self.drafts[item.id] = item.text
        _, errors = self.claims.compile(item.text, now)
        if errors:
            item.status = Status.NEEDS_EVIDENCE
            item.notes += errors
            self.cockpit.notify(item, "Draft needs evidence")
            return
        item.forecast_seq = self.forecasts.commit(
            item.id, {"impressions": 500.0, "approval": 0.7},
            {"impressions": 300.0, "approval": 0.5}, now)
        item.status = Status.REVIEW
        self.cockpit.notify(item, "Ready for review")

    def edit(self, item: Item, text: str) -> None:
        """Any human edit voids approval and returns the item to review."""
        item.text = text
        self.passports.pop(item.id, None)
        if item.status in (Status.APPROVED, Status.SCHEDULED):
            item.status = Status.REVIEW

    def approve(self, item: Item, approver: str, now: datetime) -> Passport:
        claim_ids, errors = self.claims.compile(item.text, now)
        if errors:
            raise ValueError("; ".join(errors))
        self.taste.record_edit(self.drafts.get(item.id, item.text), item.text)
        passport = Passport(item.id, approver, now,
                            content_hash(item, self.taste.version, self.claims, claim_ids),
                            self.taste.version, tuple(claim_ids))
        self.passports[item.id] = passport
        item.status = Status.APPROVED
        return passport

    def _schedule(self, item: Item, now: datetime) -> None:
        """Scheduling only: nothing here can send. The publisher re-checks everything at send time."""
        errors = preflight(item)
        passport = self.passports.get(item.id)
        if self.brand.paused:
            errors.append("brand paused")
        if item.channel not in self.brand.channels:
            errors.append("channel not allowed")
        if passport is None:
            errors.append("no approval")
        else:
            errors += [p for cid in passport.claim_ids if (p := self.claims.problem(cid, now))]
        if errors:
            item.status = Status.FAILED
            self.cockpit.notify(item, "; ".join(errors))
            return
        item.status = Status.SCHEDULED

    def _due(self, item: Item) -> None:
        if item.id not in self.announced:
            self.announced.add(item.id)
            self.cockpit.notify(item, DUE)

    def _measure(self, item: Item, now: datetime) -> None:
        for day in (2, 7):
            if day not in item.metrics_pulled and now >= item.publish_at + timedelta(days=day):
                self.metrics.add(item.brand, item.channel, item.id, day,
                                 self.metrics_source.pull(item), now)
                item.metrics_pulled.add(day)
                if day == 7 and item.forecast_seq:
                    self.forecasts.score(item.forecast_seq, self.metrics.latest(item.id))
