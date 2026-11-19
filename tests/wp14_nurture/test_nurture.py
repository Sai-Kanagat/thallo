"""Nurture on the lead desk's one send path: hash-approved templates, one draft per step, a tap per
message, quiet hours, caps per person, stop on reply, withdrawal or leaving nurture. In memory."""
from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from tests.wp13_leads.conftest import FakeSender
from thallo.contracts.enums import ConsentBasis, Language, LeadRoute, MessageChannel, MessageKind, TouchKind
from thallo.contracts.models import Consent, Lead, Message
from thallo.contracts.states import LeadState, MessageState
from thallo.leads import DeskError, TouchEvent, body_hash
from thallo.nurture import nurture_desk
from thallo.nurture.records import EnrolmentState, NurtureEnrolment, NurtureStepDraft

ROOT = Path(__file__).resolve().parents[2]
STEP1 = "Ciao {{name}}, ti va di passare a vedere il {{model}}? Siamo in negozio e ti aspettiamo."
STEP2 = "Ciao {{name}}, se vuoi provare il {{model}} dimmi quando passi."
PHONE = "+39 333 123 4567"
ANSWERS = {"model": "Leoncino 500", "payment": "cash", "town": "Bologna", "timing": "this_week"}


def _event(w, **kw):
    base = dict(kind=TouchKind.FORM, occurred_at=w.clock.now(), display_name="Marco Rossi", phone=PHONE)
    return TouchEvent(**(base | kw))


def setup(w, tx, *, quiet=(22, 6), max_per_week=2, gap=24, waits=(0, 48)):
    d, n = nurture_desk(tx, w.tenant.id, w.clock)
    person = d._person(_event(w))
    d.record_consent(person.id, channel=MessageChannel.WHATSAPP, recipient=PHONE, basis=ConsentBasis.ENQUIRY,
                     evidence="form with WhatsApp opt in", actor=w.human(w.seller), verifier=w.verifier)
    lead = d.intake(_event(w))
    lead = d.record_answers(lead.id, ANSWERS)
    assert lead.route is LeadRoute.WARM and lead.state is LeadState.ROUTED
    ids = []
    for i, body in enumerate((STEP1, STEP2)):
        t = n.new_template(name=f"n{i}", channel=MessageChannel.WHATSAPP, language=Language.IT, body=body,
                           slot_sources={"name": "person.display_name", "model": "answer.model"})
        n.approve_template(t.id, actor=w.human(w.founder), verifier=w.verifier)
        ids.append(t.id)
    seq = n.new_sequence(name="warm", channel=MessageChannel.WHATSAPP, steps=list(zip(ids, waits, strict=True)),
                         quiet_start_hour=quiet[0], quiet_end_hour=quiet[1], max_per_week=max_per_week,
                         min_gap_hours=gap)
    e = n.enrol(lead.id, seq.id)
    return d, n, lead, e


def _nurture_msgs(tx):
    return [m for m in tx.list_all(Message) if m.kind is MessageKind.NURTURE]


def _send(w, d, m, sender=None):
    return d.approve_and_send(m.id, actor=w.human(w.seller), verifier=w.verifier, sender=sender or FakeSender("sent"),
                              body_sha256=body_hash(m.body))


def test_due_step_is_drafted_once_and_waits_for_a_tap(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx)
        assert tx.get(Lead, lead.id).state is LeadState.NURTURING
        assert n.run_due()["drafted"] == 1
        assert n.run_due()["drafted"] == 0  # idempotent
        [m] = _nurture_msgs(tx)
        assert m.state is MessageState.DRAFTED and "Marco" in m.body and "Leoncino 500" in m.body
        assert m.template_approval_id and "STOP" in m.body  # the consent footer from the policy
        assert len(tx.list_all(NurtureStepDraft)) == 1
        out = _send(w, d, m)
        assert out["status"] == "sent"
        assert tx.get(Lead, lead.id).state is LeadState.NURTURING  # a nurture send does not leave nurture


def test_next_step_only_after_the_wait_from_the_sent_one(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx)
        n.run_due()
        _send(w, d, _nurture_msgs(tx)[0])
        w.clock.advance(hours=47)
        assert n.run_due()["drafted"] == 0
        w.clock.advance(hours=2)
        assert n.run_due()["drafted"] == 1
        m2 = [m for m in _nurture_msgs(tx) if m.state is MessageState.DRAFTED][0]
        _send(w, d, m2)
        w.clock.advance(hours=1)
        assert n.run_due()["done"] == 1
        assert tx.get(NurtureEnrolment, e.id).state is EnrolmentState.DONE


def test_quiet_hours_defer_drafting_and_refuse_the_tap(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx, quiet=(8, 12))  # epoch is 09:00 UTC, tenant zone UTC
        assert n.run_due()["deferred"] == 1 and not _nurture_msgs(tx)
        assert tx.get(NurtureEnrolment, e.id).due_at.hour == 12
        w.clock.advance(hours=3)
        assert n.run_due()["drafted"] == 1


def test_frequency_cap_counts_every_message_to_the_person(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx, max_per_week=1, gap=1, waits=(0, 1))
        n.run_due()
        _send(w, d, _nurture_msgs(tx)[0])
        w.clock.advance(hours=2)
        assert n.run_due()["deferred"] == 1  # one a week
        w.clock.advance(days=7)
        assert n.run_due()["drafted"] == 1


def test_a_reply_stops_the_sequence_and_discards_its_drafts(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx)
        n.run_due()
        [m] = _nurture_msgs(tx)
        w.clock.advance(minutes=5)
        d.intake(_event(w, kind=TouchKind.WHATSAPP_CODE))  # the person writes back
        with pytest.raises(DeskError, match="stopped"):
            _send(w, d, m)
        assert tx.get(NurtureEnrolment, e.id).stop_reason == "replied"
        assert tx.get(Message, m.id).state is MessageState.DISCARDED


def test_withdrawal_stops_the_sequence(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx)
        for c in tx.list_all(Consent):
            tx.update(Consent, c.id, withdrawn_at=w.clock.now())
        w.clock.advance(minutes=1)
        assert n.run_due()["stopped"] == 1
        assert tx.get(NurtureEnrolment, e.id).stop_reason == "withdrawn"
        assert not _nurture_msgs(tx)


def test_plain_lead_desk_refuses_nurture_and_nobody_edits_approved_wording(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx)
        n.run_due()
        [m] = _nurture_msgs(tx)
        plain = w.desk(tx)  # no nurture guard: fail closed
        with pytest.raises(DeskError, match="only acknowledgements"):
            _send(w, plain, m)
        with pytest.raises(DeskError, match="approved wording"):
            d.edit_draft(m.id, "Ciao, ultimo prezzo per te")
        tampered = m.body.replace("ti aspettiamo", "ti faccio lo sconto")
        tx.update(Message, m.id, body=tampered)
        with pytest.raises(DeskError, match="approved template"):
            d.approve_and_send(m.id, actor=w.human(w.seller), verifier=w.verifier, sender=FakeSender("sent"),
                               body_sha256=body_hash(tampered))


def test_template_wording_is_checked_and_needs_a_person(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        _d, n = nurture_desk(tx, w.tenant.id, w.clock)
        with pytest.raises(DeskError, match="promises"):
            n.new_template(name="x", channel=MessageChannel.WHATSAPP, language=Language.IT,
                           body="Sconto del 10% solo oggi", slot_sources={})
        t = n.new_template(name="y", channel=MessageChannel.WHATSAPP, language=Language.IT, body=STEP2,
                           slot_sources={"name": "person.display_name", "model": "answer.model"})
        with pytest.raises(DeskError, match="founder"):
            n.approve_template(t.id, actor=w.human(w.seller), verifier=w.verifier)


def test_only_warm_routed_leads_with_consent_enrol(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx)
        with pytest.raises(DeskError, match="already|warm"):
            n.enrol(lead.id, e.sequence_id)


def test_nurture_never_builds_a_send_request():
    for path in (ROOT / "thallo" / "nurture").glob("*.py"):
        tree = ast.parse(path.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "MessageSendRequest" not in names, path.name
        assert "TextRequest" not in names, path.name  # no model calls: CRM and lead data never reach a prompt


def test_tick_runs_twice_without_duplicates_after_time_moves(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, n, lead, e = setup(w, tx)
        n.run_due()
        w.clock.advance(timedelta(days=3))
        n.run_due()
        assert len(_nurture_msgs(tx)) == 1  # step two waits for the tap on step one
