"""Builds a tenant's history row by row, the same on the memory backend and on Postgres.

Every number here is invented test data. A piece is one published variant; each lead hangs off a
touch on that piece (or another piece for last touch), and outcomes are recorded through the
core rows the lead desk writes (booking, deal, outcome).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from thallo.contracts.enums import (
    ApprovalMethod,
    ApprovalSubject,
    Channel,
    FunnelStage,
    Language,
    LeadRoute,
    OutcomeKind,
    TouchKind,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Approval,
    Asset,
    Booking,
    Brief,
    Bundle,
    Deal,
    GrowthModel,
    Idea,
    Lead,
    Outcome,
    Person,
    Pillar,
    Publication,
    Touch,
    Variant,
)

WON_NO_AMOUNT = "missing"


@dataclass
class LeadSpec:
    route: LeadRoute | None = LeadRoute.WARM
    booked: bool = False
    won: Decimal | str | None = None  # an amount, WON_NO_AMOUNT, or None for not won
    last_touch_on: str | None = None  # a variant id for a different last touch
    self_report: str | None = None


@dataclass
class Seeder:
    tx: Any
    clock: Any
    approver_id: str
    pillars: dict[str, Pillar] = field(default_factory=dict)

    def _now(self) -> datetime:
        return self.clock.now()

    def pillar_set(self, shares: dict[str, str]) -> dict[str, Pillar]:
        t = self.tx.tenant_id
        gm = self.tx.insert(GrowthModel(id=new_id(self.clock), created_at=self._now(), tenant_id=t, version=1,
                                        active=True))
        for name, share in shares.items():
            self.pillars[name] = self.tx.insert(Pillar(
                id=new_id(self.clock), created_at=self._now(), tenant_id=t, growth_model_id=gm.id, name=name,
                stage=FunnelStage.CONSIDERATION, target_share=Decimal(share)))
        return self.pillars

    def piece(self, *, pillar: str | None, channel: Channel = Channel.INSTAGRAM, days_ago: int = 30,
              edited: bool = False, prompt_version: str | None = None) -> Variant:
        t, now = self.tx.tenant_id, self._now()
        idea = self.tx.insert(Idea(id=new_id(self.clock), created_at=now, tenant_id=t, title="i", rationale="r",
                                   stage=FunnelStage.CONSIDERATION, prompt_version=prompt_version,
                                   pillar_id=self.pillars[pillar].id if pillar else None))
        brief = self.tx.insert(Brief(id=new_id(self.clock), created_at=now, tenant_id=t, idea_id=idea.id,
                                     audience="a", job_to_be_done="j", message="m", call_to_action="c",
                                     language=Language.EN, channels=[channel]))
        asset = self.tx.insert(Asset(id=new_id(self.clock), created_at=now, tenant_id=t, brief_id=brief.id,
                                     master_text="t"))
        bundle = self.tx.insert(Bundle(id=new_id(self.clock), created_at=now, tenant_id=t, asset_id=asset.id,
                                       root_hash="a" * 64, rule_version=1))
        v = self.tx.insert(Variant(id=new_id(self.clock), created_at=now, tenant_id=t, bundle_id=bundle.id,
                                   channel=channel, language=Language.EN, text="t", task_type="post"))
        ap = self.tx.insert(Approval(id=new_id(self.clock), created_at=now, tenant_id=t,
                                     subject_kind=ApprovalSubject.BUNDLE, subject_id=bundle.id,
                                     content_hash="a" * 64, approver_id=self.approver_id,
                                     method=ApprovalMethod.PER_BUNDLE, approved_at=now,
                                     expires_at=datetime.now(UTC) + timedelta(days=365)))
        self.tx.insert(Publication(id=new_id(self.clock), created_at=now, tenant_id=t, variant_id=v.id,
                                   approval_id=ap.id, idempotency_key=new_id(self.clock),
                                   content_sha256="b" * 64, sent_at=now - timedelta(days=days_ago)))
        if edited:
            from thallo.contracts.models import Edit

            self.tx.insert(Edit(id=new_id(self.clock), created_at=now, tenant_id=t, variant_id=v.id,
                                editor_id=self.approver_id, before_text="a", after_text="b"))
        return v

    def touch(self, variant_id: str | None, kind: TouchKind = TouchKind.FORM, person_id: str | None = None,
              self_report: str | None = None) -> Touch:
        return self.tx.insert(Touch(id=new_id(self.clock), created_at=self._now(), tenant_id=self.tx.tenant_id,
                                    kind=kind, variant_id=variant_id, person_id=person_id, self_report=self_report,
                                    occurred_at=self._now() - timedelta(days=5)))

    def lead(self, variant_id: str | None, spec: LeadSpec) -> Lead:
        t, now = self.tx.tenant_id, self._now()
        person = self.tx.insert(Person(id=new_id(self.clock), created_at=now, tenant_id=t))
        first = self.touch(variant_id, person_id=person.id)
        last = self.touch(spec.last_touch_on, TouchKind.LINK, person.id) if spec.last_touch_on else first
        if spec.self_report:
            self.touch(None, TouchKind.SELF_REPORT, person.id, spec.self_report)
        lead = self.tx.insert(Lead(id=new_id(self.clock), created_at=now - timedelta(days=5), tenant_id=t,
                                   person_id=person.id, route=spec.route, first_touch_id=first.id,
                                   last_touch_id=last.id))
        if spec.booked or spec.won is not None:
            b = self.tx.insert(Booking(id=new_id(self.clock), created_at=now, tenant_id=t, lead_id=lead.id,
                                       kind="demo", starts_at=now - timedelta(days=3)))
            if spec.won is not None:
                d = self.tx.insert(Deal(id=new_id(self.clock), created_at=now, tenant_id=t, booking_id=b.id,
                                        lead_id=lead.id, value=Decimal("99999")))
                amount = None if spec.won == WON_NO_AMOUNT else Decimal(spec.won)
                self.tx.insert(Outcome(id=new_id(self.clock), created_at=now, tenant_id=t, deal_id=d.id,
                                       kind=OutcomeKind.WON, amount=amount, recorded_by=self.approver_id,
                                       occurred_at=now))
        return lead
