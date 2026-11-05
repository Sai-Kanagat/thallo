"""The weekly state machine. Call tick() on a schedule; it is safe to repeat."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cortex_operator.analytics import MetricStore
from cortex_operator.contracts import (BrandSettings, Cockpit, Item, MetricsSource, Passport,
                                       Publisher, Status, Writer, week_offsets)
from cortex_operator.foresight import ForecastLedger
from cortex_operator.gate import Gate, content_hash
from cortex_operator.publishing import deliver, preflight
from cortex_operator.taste import TasteLedger
from cortex_operator.truth import ClaimLedger


@dataclass
class Engine:
    brand: BrandSettings
    cockpit: Cockpit
    writer: Writer
    publisher: Publisher
    metrics_source: MetricsSource
    claims: ClaimLedger
    taste: TasteLedger = field(default_factory=TasteLedger)
    forecasts: ForecastLedger = field(default_factory=ForecastLedger)
    metrics: MetricStore = field(default_factory=MetricStore)
    gate: Gate | None = None
    passports: dict[str, Passport] = field(default_factory=dict)
    drafts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.gate = self.gate or Gate(self.claims)

    def tick(self, now: datetime) -> None:
        for item in self.cockpit.items():
            times = week_offsets(item.publish_at)
            if item.status is Status.IDEA and now >= times["generate"]:
                self._generate(item, now)
            elif item.status is Status.REVIEW and now >= times["hold"]:
                item.status = Status.ON_HOLD
                self.cockpit.notify(item, "Not approved in time, slot on hold")
            elif item.status is Status.APPROVED and now >= times["schedule"]:
                self._schedule(item, now)
            elif item.status is Status.SCHEDULED and now >= item.publish_at:
                self._publish(item, now)
            elif item.status is Status.PUBLISHED:
                self._measure(item, now)

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
        errors = preflight(item)
        decision = self.gate.evaluate(item, self.passports.get(item.id), self.brand, now)
        if errors or not decision.allowed:
            item.status = Status.FAILED
            self.cockpit.notify(item, "; ".join(errors) or decision.reason)
            return
        item.status = Status.SCHEDULED

    def _publish(self, item: Item, now: datetime) -> None:
        decision = self.gate.evaluate(item, self.passports.get(item.id), self.brand, now)
        if not decision.allowed:
            item.status = Status.FAILED
            self.cockpit.notify(item, decision.reason)
            return
        url = deliver(self.publisher, f"{item.brand}:{item.id}", item)
        if url is None:
            item.status = Status.FAILED
            self.cockpit.notify(item, "Delivery failed, reschedule needed")
            return
        item.url, item.status = url, Status.PUBLISHED
        self.cockpit.notify(item, f"Live: {url}")

    def _measure(self, item: Item, now: datetime) -> None:
        for day in (2, 7):
            if day not in item.metrics_pulled and now >= item.publish_at + timedelta(days=day):
                self.metrics.add(item.brand, item.channel, item.id, day,
                                 self.metrics_source.pull(item), now)
                item.metrics_pulled.add(day)
                if day == 7 and item.forecast_seq:
                    self.forecasts.score(item.forecast_seq, self.metrics.latest(item.id))
