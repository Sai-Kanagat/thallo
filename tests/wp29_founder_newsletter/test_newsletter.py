"""Newsletter: winners by forecast, the issue through copy production, consent at send time, unsubscribe."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.wp13_leads.conftest import FakeSender, world  # noqa: F401 - fixture
from tests.wp29_founder_newsletter.conftest import POST
from thallo.contracts.actors import HumanOnlyViolation
from thallo.contracts.enums import Channel, FunnelStage, Language, MessageChannel, MetricDay
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Asset,
    Brief,
    Bundle,
    Consent,
    Forecast,
    ForecastScore,
    Idea,
    Person,
    Publication,
    Variant,
)
from thallo.contracts.states import VariantState
from thallo.extensions.newsletter import (
    NO_WINNERS,
    NewsletterDelivery,
    NewsletterIssue,
    SendRefused,
    draft_issue,
    send_issue,
    subscribe,
    unsubscribe,
    winners,
)
from thallo.gate.canonical import PublishPayload
from thallo.gate.decision import GateDecision

BASE = "https://shop.example.invalid/unsubscribe"


def piece(tx, clock, tenant_id, *, actual, high=10.0, published=True, text=POST):
    now = clock.now()
    idea = tx.insert(Idea(id=new_id(clock), created_at=now, tenant_id=tenant_id, stage=FunnelStage.AWARENESS,
                          title="t", rationale="r"))
    brief = tx.insert(Brief(id=new_id(clock), created_at=now, tenant_id=tenant_id, idea_id=idea.id, audience="a",
                            job_to_be_done="j", message="m", call_to_action="c", language=Language.EN))
    asset = tx.insert(Asset(id=new_id(clock), created_at=now, tenant_id=tenant_id, brief_id=brief.id, master_text=text))
    bundle = tx.insert(Bundle(id=new_id(clock), created_at=now, tenant_id=tenant_id, asset_id=asset.id, rule_version=1))
    v = tx.insert(Variant(id=new_id(clock), created_at=now, tenant_id=tenant_id, bundle_id=bundle.id,
                          channel=Channel.INSTAGRAM, language=Language.EN, state=VariantState.PUBLISHED, text=text,
                          task_type="post"))
    if published:
        tx.insert(Publication(id=new_id(clock), created_at=now, tenant_id=tenant_id, variant_id=v.id,
                              approval_id=new_id(clock), idempotency_key=new_id(clock), content_sha256="0" * 64,
                              sent_at=now))
    f = tx.insert(Forecast(id=new_id(clock), created_at=now, tenant_id=tenant_id, brief_id=brief.id, seq=1,
                           metric="link_clicks", day=list(MetricDay)[-1], low=2.0, high=high, baseline_low=1.0,
                           baseline_high=8.0, sealed_at=now, seal_hash="0" * 64))
    tx.insert(ForecastScore(id=new_id(clock), created_at=now, tenant_id=tenant_id, forecast_id=f.id, actual=actual,
                            scored_at=now))
    return brief.id


def test_winners_only_pieces_above_their_forecast(world):  # noqa: F811
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        win = piece(tx, w.clock, w.tenant.id, actual=15.0)
        piece(tx, w.clock, w.tenant.id, actual=9.0)  # inside the interval
        piece(tx, w.clock, w.tenant.id, actual=None)  # missing is not zero and not a win
        piece(tx, w.clock, w.tenant.id, actual=30.0, published=False)
        picks = winners(tx, period_end=w.clock.now().date())
        assert [p.brief_id for p in picks] == [win]
        assert winners(tx, period_end=w.clock.now().date() + timedelta(days=30)) == []


def test_no_winners_no_issue_no_call(world, clock, vault):  # noqa: F811
    out = draft_issue(registry=None, tenant=world.tenant, picks=[], period_end=date(2026, 11, 20), claims={},
                      clock=clock, resolver=lambda u: None, non_links=())
    assert out.status == NO_WINNERS and out.rows() == []


def test_issue_is_drafted_on_the_newsletter_channel(world, registry_for):  # noqa: F811
    w = world
    tenant = w.tenant.model_copy(update={"default_language": Language.EN})
    with w.backend.tenant(w.tenant.id) as tx:
        piece(tx, w.clock, w.tenant.id, actual=15.0)
        picks = winners(tx, period_end=w.clock.now().date())
    import tests.wp29_founder_newsletter.conftest as cf
    reg, script = registry_for(POST, POST, POST)
    # the scripted registry is configured for the samples tenant id
    tenant = tenant.model_copy(update={"id": cf.T})
    picks = [p.__class__(p.brief_id, p.variant.model_copy(update={"tenant_id": cf.T}), p.claim_ids, p.margin)
             for p in picks]
    out = draft_issue(registry=reg, tenant=tenant, picks=picks, period_end=w.clock.now().date(), claims={},
                      clock=w.clock, resolver=lambda u: None, non_links=())
    assert out.variant.channel is Channel.NEWSLETTER and out.variant.state is VariantState.CHECKING
    assert out.issue.piece_brief_ids == [picks[0].brief_id]
    assert POST not in script.system(0)


def setup_issue(w):
    with w.backend.tenant(w.tenant.id) as tx:
        now = w.clock.now()
        issue = tx.insert(NewsletterIssue(id=new_id(w.clock), created_at=now, tenant_id=w.tenant.id,
                                          period_start=date(2026, 11, 1), period_end=date(2026, 11, 14),
                                          variant_id=new_id(w.clock), piece_brief_ids=[new_id(w.clock)]))
        people = []
        for i in range(3):
            p = tx.insert(Person(id=new_id(w.clock), created_at=now, tenant_id=w.tenant.id,
                                 email=f"reader{i}@example.com"))
            people.append(p)
        subs = [subscribe(tx, tenant_id=w.tenant.id, clock=w.clock, person_id=p.id, email=p.email,
                          evidence="ticked the box on the website form", actor=w.human(w.founder), verifier=w.verifier)
                for p in people[:2]]  # the third person never subscribed
    return issue, subs, people


def decision_for(w, issue, *, channel="newsletter", allowed=True):
    payload = PublishPayload(variant_id=issue.variant_id, variant_hash="0" * 64, channel=channel, language="en",
                             text="The best of the fortnight.", media=(), links=(), tracked_links=())
    return GateDecision(allowed=allowed, blocks=(), tenant_id=w.tenant.id, bundle_id=new_id(w.clock),
                        variant_id=issue.variant_id, approval_id=new_id(w.clock),
                        payload=payload if allowed else None)


def test_send_only_to_consented_subscribers_with_unsubscribe(world):  # noqa: F811
    w = world
    issue, subs, _ = setup_issue(w)
    sender = FakeSender()
    with w.backend.tenant(w.tenant.id) as tx:
        assert unsubscribe(tx, token=subs[1].token, clock=w.clock)
        assert tx.get(Consent, subs[1].consent_id).withdrawn_at is not None
        rep = send_issue(tx, tenant_id=w.tenant.id, clock=w.clock, issue_id=issue.id, decision=decision_for(w, issue),
                         actor=w.human(w.founder), verifier=w.verifier, sender=sender, unsubscribe_base=BASE)
    assert [r.to for r in sender.requests] == ["reader0@example.com"]
    req = sender.requests[0]
    assert req.channel is MessageChannel.EMAIL and req.human is not None and req.consent_id == subs[0].consent_id
    assert req.body.startswith("The best of the fortnight.") and f"{BASE}/{subs[0].token}" in req.body
    assert rep.skipped == [(subs[1].id, "unsubscribed")] and len(rep.opened) == 1
    # a second tap sends nobody twice
    with w.backend.tenant(w.tenant.id) as tx:
        again = send_issue(tx, tenant_id=w.tenant.id, clock=w.clock, issue_id=issue.id,
                           decision=decision_for(w, issue), actor=w.human(w.founder), verifier=w.verifier,
                           sender=sender, unsubscribe_base=BASE)
        assert len(tx.list_all(NewsletterDelivery)) == 1
    assert len(sender.requests) == 1 and (subs[0].id, "already_tapped") in again.skipped


def test_withdrawn_consent_at_send_time_is_skipped(world):  # noqa: F811
    w = world
    issue, subs, _ = setup_issue(w)
    sender = FakeSender()
    with w.backend.tenant(w.tenant.id) as tx:
        tx.update(Consent, subs[0].consent_id, withdrawn_at=w.clock.now())
        rep = send_issue(tx, tenant_id=w.tenant.id, clock=w.clock, issue_id=issue.id, decision=decision_for(w, issue),
                         actor=w.human(w.founder), verifier=w.verifier, sender=sender, unsubscribe_base=BASE)
    assert [r.to for r in sender.requests] == ["reader1@example.com"]
    assert rep.skipped[0][0] == subs[0].id


def test_refusals(world):  # noqa: F811
    w = world
    issue, _, _ = setup_issue(w)
    sender = FakeSender()
    kw = dict(tenant_id=w.tenant.id, clock=w.clock, issue_id=issue.id, verifier=w.verifier, sender=sender)
    with w.backend.tenant(w.tenant.id) as tx:
        with pytest.raises(HumanOnlyViolation):
            send_issue(tx, decision=decision_for(w, issue), actor=w.human(w.founder, "stale"),
                       unsubscribe_base=BASE, **kw)
        with pytest.raises(SendRefused):
            send_issue(tx, decision=decision_for(w, issue, allowed=False), actor=w.human(w.founder),
                       unsubscribe_base=BASE, **kw)
        with pytest.raises(SendRefused):
            send_issue(tx, decision=decision_for(w, issue, channel="instagram"), actor=w.human(w.founder),
                       unsubscribe_base=BASE, **kw)
        with pytest.raises(SendRefused):
            send_issue(tx, decision=decision_for(w, issue), actor=w.human(w.founder),
                       unsubscribe_base="http://plain.example", **kw)
        assert not unsubscribe(tx, token="nope", clock=w.clock)
    assert sender.requests == []

