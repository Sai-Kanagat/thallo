"""The lead desk end to end in memory: intake, consent before drafting, acknowledgement by hash,
qualification, two numbers, routes, a human tap per message, clock and outcome buttons."""
from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.wp13_leads.conftest import FakeSender
from thallo.contracts.actors import AgentActor, HumanOnlyViolation
from thallo.contracts.enums import (
    ConsentBasis,
    ConsentScope,
    Language,
    LeadRoute,
    LossReason,
    MessageChannel,
    MessageKind,
    TouchKind,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import Consent, Deal, Lead, Message, Outcome, Person
from thallo.contracts.states import LeadState, MessageState
from thallo.leads import DeskError, FakeTouchFeed, TouchEvent, body_hash, safe_link
from thallo.leads.records import LeadMessageSend, SendState

ROOT = Path(__file__).resolve().parents[2]
ACK = "Ciao {{name}}, abbiamo ricevuto il tuo messaggio sul {{model}}. Ti risponde una persona del negozio."


def _consent(w, tx, person_id, **kw):
    base = dict(basis=ConsentBasis.ENQUIRY, channel=MessageChannel.WHATSAPP, scope=ConsentScope.CONTACT,
                evidence="WhatsApp chat started by the person", given_at=w.clock.now())
    tx.insert(Consent(id=new_id(w.clock), created_at=w.clock.now(), tenant_id=w.tenant.id, person_id=person_id,
                      **(base | kw)))


def _event(w, **kw):
    base = dict(kind=TouchKind.WHATSAPP_CODE, occurred_at=w.clock.now(), display_name="Marco Rossi",
                phone="+39 333 123 4567", code="LEO-7")
    return TouchEvent(**(base | kw))


def _with_ack(w, tx):
    d = w.desk(tx)
    t = d.new_template(name="ack", channel=MessageChannel.WHATSAPP, language=Language.IT, body=ACK,
                       slot_sources={"name": "person.display_name", "model": "answer.model"})
    d.approve_template(t.id, actor=w.human(w.founder), verifier=w.verifier)
    return t


def _consented_lead(w, tx, **kw):
    """Consent exists before the touch (an opt-in on file), so the ack is drafted at intake."""
    d = w.desk(tx)
    person = d._person(_event(w, **kw))
    _consent(w, tx, person.id)
    return d.intake(_event(w, **kw))


# ---------------------------------------------------------------- intake and consent

def test_no_consent_basis_means_no_draft_at_all(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        _with_ack(w, tx)
        d = w.desk(tx)
        lead = d.intake(_event(w))
        lead = d.record_answers(lead.id, {"model": "Leoncino 500", "payment": "cash", "town": "Bologna",
                                          "timing": "this_week"})
        assert lead.route is LeadRoute.HOT
        assert tx.list_all(Message) == []  # personal reply only
        assert d.clock_status(lead)["running"]


def test_intake_matches_person_and_drafts_the_hash_approved_ack(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        _with_ack(w, tx)
        lead = _consented_lead(w, tx)
        msgs = tx.list_all(Message)
        assert len(msgs) == 1 and msgs[0].kind is MessageKind.ACKNOWLEDGEMENT
        assert msgs[0].state is MessageState.DRAFTED and msgs[0].template_approval_id
        assert msgs[0].body.startswith("Ciao Marco, abbiamo ricevuto il tuo messaggio sul.")
        assert msgs[0].body.endswith("Rispondi STOP per non ricevere altri messaggi")
        again = w.desk(tx).intake(_event(w, phone="00393331234567", kind=TouchKind.CALL))
        assert again.id == lead.id and len(tx.list_all(Person)) == 1
        assert len(tx.list_all(Message)) == 1  # one acknowledgement per lead
        assert again.heat == pytest.approx(65.0)


def test_unapproved_or_changed_template_drafts_nothing(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        t = _with_ack(w, tx)
        tx.update(type(t), t.id, body=t.body + " Promo!")  # wording changed after approval
        _consented_lead(w, tx)
        assert tx.list_all(Message) == []


def test_template_approval_is_human_only(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        t = d.new_template(name="ack", channel=MessageChannel.WHATSAPP, language=Language.IT, body=ACK,
                           slot_sources={"name": "person.display_name", "model": "answer.model"})
        with pytest.raises(HumanOnlyViolation):
            d.approve_template(t.id, actor=AgentActor(id="agent-1"), verifier=w.verifier)
        with pytest.raises(HumanOnlyViolation):
            d.approve_template(t.id, actor=w.human(w.founder, "stale"), verifier=w.verifier)
        with pytest.raises(DeskError, match="founder, approver or operator"):
            d.approve_template(t.id, actor=w.human(w.seller), verifier=w.verifier)
        with pytest.raises(DeskError, match="promises"):
            d.new_template(name="bad", channel=MessageChannel.WHATSAPP, language=Language.IT,
                           body="Sconto del 10% solo oggi", slot_sources={})


# ---------------------------------------------------------------- qualification and routing

def test_questions_scores_and_routes(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        lead = _consented_lead(w, tx)
        lead = d.record_answers(lead.id, {"payment": "finance"})
        assert lead.state is LeadState.QUALIFYING and lead.route is None
        with pytest.raises(DeskError, match="one of"):
            d.record_answers(lead.id, {"payment": "bitcoin"})
        with pytest.raises(DeskError, match="not a question"):
            d.record_answers(lead.id, {"salary": "x"})
        lead = d.record_answers(lead.id, {"model": "Leoncino 500", "down_payment_ready": "yes",
                                          "town": "Imola", "timing": "this_month"})
        assert lead.fit == 10 + 20 + 25 + 20 + 15 and lead.heat == 30
        assert lead.route is LeadRoute.HOT and lead.state is LeadState.ROUTED
        drafts = [m for m in tx.list_all(Message) if m.kind is MessageKind.REPLY_DRAFT]
        assert len(drafts) == 1 and "Leoncino 500" in drafts[0].body


def test_low_fit_is_parked_with_no_outreach(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        lead = _consented_lead(w, tx)
        lead = d.record_answers(lead.id, {"model": "Other", "payment": "not_sure", "town": "Milano",
                                          "timing": "later"})
        assert lead.route is LeadRoute.LOW_FIT and lead.state is LeadState.PARKED
        assert not [m for m in tx.list_all(Message) if m.kind is MessageKind.REPLY_DRAFT]
        assert d.draft_reply(lead.id) is None


def test_untrusted_lead_text_is_capped_and_cleaned(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        lead = d.intake(_event(w, display_name="Ann‮" + "y" * 300))
        lead = d.record_answers(lead.id, {"town": "Bo\x00logna " + "z" * 500})
        assert "\x00" not in lead.answers["town"] and len(lead.answers["town"]) == 200
        assert "‮" not in d.person(lead).display_name and len(d.person(lead).display_name) == 80


def test_the_fake_touch_feed_stands_in_for_wp12(world):
    w = world
    feed = FakeTouchFeed()
    feed.push(w.tenant.id, _event(w, kind=TouchKind.FORM, phone=None, email="anna@acme.it",
                                  answers={"timing": "this_week", "ignored": "x"}))
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        for ev in feed.pending(w.tenant.id):
            lead = d.intake(ev)
            feed.taken(w.tenant.id, ev)
        assert feed.pending(w.tenant.id) == []
        assert lead.answers == {"timing": "this_week"} and lead.heat == 25


# ---------------------------------------------------------------- sending: one human tap per message

def _hot(w, tx):
    _with_ack(w, tx)
    d = w.desk(tx)
    lead = _consented_lead(w, tx)
    d.record_answers(lead.id, {"model": "Leoncino 500", "payment": "cash", "town": "Bologna", "timing": "this_week"})
    return next(m for m in tx.list_all(Message) if m.kind is MessageKind.REPLY_DRAFT)


def test_manual_send_opens_the_sellers_app_and_counts_only_when_confirmed(world):
    w = world
    sender = FakeSender()
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        msg = _hot(w, tx)
        out = d.approve_and_send(msg.id, actor=w.human(w.seller), verifier=w.verifier, sender=sender,
                                 body_sha256=body_hash(msg.body))
        assert out["status"] == "open_in_app" and out["link"].startswith("https://wa.me/")
        req = sender.requests[0]
        assert req.human.id == w.seller.id and req.template_approval_id is None and req.consent_id
        assert req.to == "+393331234567"
        assert tx.get(Message, msg.id).state is MessageState.DRAFTED  # not sent until the person says so
        assert tx.get(LeadMessageSend, msg.id).state is SendState.OPENED
        with pytest.raises(DeskError, match="already approved"):
            d.approve_and_send(msg.id, actor=w.human(w.seller), verifier=w.verifier, sender=sender,
                               body_sha256=body_hash(msg.body))
        d.confirm_sent(msg.id, actor=w.human(w.seller), verifier=w.verifier)
        sent = tx.get(Message, msg.id)
        assert sent.state is MessageState.SENT and sent.sent_by_user_id == w.seller.id
        lead = tx.get(Lead, msg.lead_id)
        assert lead.state is LeadState.CONTACTED and lead.first_response_at is not None
        assert not d.clock_status(lead)["running"]


def test_send_refuses_machines_changed_bodies_and_withdrawn_consent(world):
    w = world
    sender = FakeSender(status="sent")
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        msg = _hot(w, tx)
        with pytest.raises(HumanOnlyViolation):
            d.approve_and_send(msg.id, actor=AgentActor(id="a"), verifier=w.verifier, sender=sender,
                               body_sha256=body_hash(msg.body))
        with pytest.raises(DeskError, match="changed"):
            d.approve_and_send(msg.id, actor=w.human(w.seller), verifier=w.verifier, sender=sender,
                               body_sha256=body_hash(msg.body + "x"))
        for cons in tx.list_all(Consent):
            tx.update(Consent, cons.id, withdrawn_at=w.clock.now())
        w.clock.advance(seconds=1)
        with pytest.raises(DeskError, match="consent"):
            d.approve_and_send(msg.id, actor=w.human(w.seller), verifier=w.verifier, sender=sender,
                               body_sha256=body_hash(msg.body))
        assert sender.requests == []


def test_provider_sent_marks_sent_and_failure_is_recorded_not_retried(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        msg = _hot(w, tx)
        out = d.approve_and_send(msg.id, actor=w.human(w.seller), verifier=w.verifier, sender=FakeSender("sent"),
                                 body_sha256=body_hash(msg.body))
        assert out["status"] == "sent" and tx.get(Message, msg.id).state is MessageState.SENT
        ack = next(m for m in tx.list_all(Message) if m.kind is MessageKind.ACKNOWLEDGEMENT)
        out = d.approve_and_send(ack.id, actor=w.human(w.seller), verifier=w.verifier,
                                 sender=FakeSender(raises=TimeoutError()), body_sha256=body_hash(ack.body))
        assert out["status"] == "failed" and tx.get(LeadMessageSend, ack.id).state is SendState.FAILED
        assert tx.get(Message, ack.id).state is MessageState.DRAFTED


def test_ack_edit_is_refused_and_reply_edit_rehashes(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        msg = _hot(w, tx)
        ack = next(m for m in tx.list_all(Message) if m.kind is MessageKind.ACKNOWLEDGEMENT)
        with pytest.raises(DeskError, match="approved wording"):
            d.edit_draft(ack.id, "anything")
        d.edit_draft(msg.id, "Ciao Marco, quando passi in negozio?")
        with pytest.raises(DeskError, match="changed"):
            d.approve_and_send(msg.id, actor=w.human(w.seller), verifier=w.verifier, sender=FakeSender(),
                               body_sha256=body_hash(msg.body))


def test_only_the_desk_builds_message_send_requests():
    """One send choke point: no other module in leads or api constructs a MessageSendRequest."""
    found = []
    for path in [*(ROOT / "thallo" / "leads").glob("*.py"), *(ROOT / "api").glob("*.py")]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "MessageSendRequest":
                found.append(path.name)
            if isinstance(node, ast.Name) and node.id == "MessageSendRequest":
                found.append(path.name)
    assert found == ["desk.py"]


def test_safe_link_allows_only_the_sellers_own_apps():
    assert safe_link("https://wa.me/393331234567?text=ciao")
    assert safe_link("mailto:a@b.it?body=x")
    for bad in ("javascript:alert(1)", "https://evil.example/", "https://wa.me/39?text=a b", None):
        assert safe_link(bad) is None


# ---------------------------------------------------------------- clock and outcomes

def test_clock_goes_overdue(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        lead = d.intake(_event(w))
        assert d.clock_status(lead)["minutes_left"] == 30
        w.clock.advance(seconds=31 * 60)
        st = d.clock_status(d.lead(lead.id))
        assert st["overdue"] and st["minutes_left"] == -1


def test_booking_and_one_tap_outcomes(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        msg = _hot(w, tx)
        d.approve_and_send(msg.id, actor=w.human(w.seller), verifier=w.verifier, sender=FakeSender("sent"),
                           body_sha256=body_hash(msg.body))
        lead_id = msg.lead_id
        with pytest.raises(DeskError, match="no open booking"):
            d.outcome(lead_id, "held", actor=w.human(w.seller), verifier=w.verifier)
        d.book(lead_id, starts_at=w.clock.now() + timedelta(days=1), kind="test drive", actor=w.human(w.seller),
               verifier=w.verifier)
        assert d.lead(lead_id).state is LeadState.BOOKED
        d.outcome(lead_id, "held", actor=w.human(w.seller), verifier=w.verifier)
        with pytest.raises(DeskError, match="amount"):
            d.outcome(lead_id, "won", actor=w.human(w.seller), verifier=w.verifier)
        lead = d.outcome(lead_id, "won", actor=w.human(w.seller), verifier=w.verifier, amount=Decimal("4200"))
        assert lead.state is LeadState.WON
        (deal,) = tx.list_all(Deal)
        (out,) = tx.list_all(Outcome)
        assert deal.value == Decimal("4200") and deal.booking_id and out.recorded_by == w.seller.id
        with pytest.raises(DeskError):
            d.outcome(lead_id, "lost", actor=w.human(w.seller), verifier=w.verifier, reason=LossReason.PRICE)


def test_lost_needs_a_reason_and_discards_open_drafts(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d = w.desk(tx)
        msg = _hot(w, tx)
        d.approve_and_send(msg.id, actor=w.human(w.seller), verifier=w.verifier, sender=FakeSender("sent"),
                           body_sha256=body_hash(msg.body))
        with pytest.raises(DeskError, match="reason"):
            d.outcome(msg.lead_id, "lost", actor=w.human(w.seller), verifier=w.verifier)
        d.outcome(msg.lead_id, "lost", actor=w.human(w.seller), verifier=w.verifier, reason=LossReason.TIMING)
        assert all(m.state is not MessageState.DRAFTED for m in tx.list_all(Message))
        assert tx.list_all(Outcome)[0].loss_reason is LossReason.TIMING
