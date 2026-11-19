"""Booking per play, the pipeline mirror, the CRM projection, CSV and the sync ledger. In memory."""
from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH
from thallo.contracts.enums import ConnectionMode, ConsentBasis, MessageChannel, TouchKind
from thallo.contracts.models import Booking, Consent, Person
from thallo.contracts.states import LeadState
from thallo.leads import DeskError, TouchEvent
from thallo.nurture import booking, crm_sync, nurture_desk
from thallo.nurture.pipeline import board
from thallo.nurture.records import CrmSyncRecord

PHONE = "+39 333 123 4567"
ANSWERS = {"model": "Leoncino 500", "payment": "cash", "town": "Bologna", "timing": "this_week"}


def _lead(w, tx, *, consent=True, name="Marco Rossi", phone=PHONE, email=None):
    d, _n = nurture_desk(tx, w.tenant.id, w.clock)
    ev = TouchEvent(kind=TouchKind.FORM, occurred_at=w.clock.now(), display_name=name, phone=phone, email=email)
    if consent:
        p = d._person(ev)
        d.record_consent(p.id, channel=MessageChannel.WHATSAPP, recipient=phone, basis=ConsentBasis.ENQUIRY,
                         evidence="form opt in", actor=w.human(w.seller), verifier=w.verifier)
    lead = d.intake(ev)
    return d, d.record_answers(lead.id, ANSWERS)


class Registry:
    def __init__(self, output, status="done", provider="fake", task=None):
        self.output, self.status, self.provider, self.task = output, status, provider, task
        self.requests = []

    def run(self, tenant_id, request):
        self.requests.append(request)
        out = self.output(request) if callable(self.output) else self.output
        res = c.RunResult(output=out, status=self.status, task=self.task, cost_eur=Decimal(0),
                          provenance=c.Provenance(made_by="provider", provider=self.provider,
                                                  mode=ConnectionMode.HOSTED, made_at=FAKE_EPOCH))
        return SimpleNamespace(result=res, provider=self.provider)

    def resolve(self, tenant_id, capability):
        return SimpleNamespace(ordered_connection_ids=("x",))

    def _get(self, tenant_id, cid):
        return SimpleNamespace(provider=self.provider)



# ---------------------------------------------------------------- booking

def test_a_booking_needs_a_play_kind_and_an_agreed_time(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, lead = _lead(w, tx)
        with pytest.raises(DeskError, match="booked conversation"):
            booking.confirm(d, lead.id, kind="demo", starts_at=w.clock.now() + timedelta(days=1),
                            actor=w.human(w.seller), verifier=w.verifier)  # demo is play B's
        with pytest.raises(DeskError, match="date and time"):
            booking.confirm(d, lead.id, kind="test_drive", starts_at=w.clock.now() + timedelta(days=400),
                            actor=w.human(w.seller), verifier=w.verifier)
        b = booking.confirm(d, lead.id, kind="test_drive", starts_at=w.clock.now() + timedelta(days=1),
                            actor=w.human(w.seller), verifier=w.verifier)
        assert b.kind == "test_drive" and d.lead(lead.id).state is LeadState.BOOKED


def test_offer_reads_slots_and_only_keeps_safe_links(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, lead = _lead(w, tx)
        soon = w.clock.now() + timedelta(hours=3)
        reg = Registry(c.BookingResult(link="https://evil.example/x", slots=(soon, w.clock.now() - timedelta(hours=1))))
        out = booking.offer(d, lead.id, reg, kind="callback")
        assert out["slots"] == [soon.isoformat()] and out["link"] is None
        assert reg.requests[0].action == "slots" and reg.requests[0].idempotency_key
        assert booking.safe_booking_link("https://cal.com/moto/visita") == "https://cal.com/moto/visita"


def test_booked_kinds_match_the_play_definitions():
    import json

    from thallo.leads.play import PLAYS_FILE

    plays = {p["code"]: p["booked_definition"].lower() for p in json.loads(PLAYS_FILE.read_text())["plays"]}
    assert set(booking.BOOKED_KINDS) == set(plays)
    for code, kinds in booking.BOOKED_KINDS.items():
        assert "date and time agreed" in plays[code]
        for k in kinds:
            assert k.split("_")[-1] in plays[code].replace("-", " ").replace("kickoff", "kickoff"), (code, k)


# ---------------------------------------------------------------- pipeline

def test_board_counts_stages_booked_and_unlogged(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, lead = _lead(w, tx)
        booking.confirm(d, lead.id, kind="callback", starts_at=w.clock.now() + timedelta(hours=1),
                        actor=w.human(w.seller), verifier=w.verifier)
        w.clock.advance(hours=20)
        b = board(tx, w.clock, "A")
        assert b["booked_conversations"] == 1 and b["unlogged_count"] == 1
        d.outcome(lead.id, "held", actor=w.human(w.seller), verifier=w.verifier)
        d.outcome(lead.id, "won", actor=w.human(w.seller), verifier=w.verifier, amount=Decimal("4200"))
        b = board(tx, w.clock, "A")
        assert b["unlogged_count"] == 0 and b["won_count"] == 1 and b["won_amount"] == "4200"
        assert {s["stage"]: s["count"] for s in b["stages"]}["won"] == 1
        assert tx.list_all(Booking)[0].state.value == "held"


# ---------------------------------------------------------------- crm

def test_projection_sends_only_consented_contact_points_and_no_free_text(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, lead = _lead(w, tx, email="marco@example.it")  # email has no consent: policy needs opt_in
        _lead(w, tx, consent=False, name="Anna Bianchi", phone="+39 333 999 0000")
        recs = crm_sync.project(d)
        marco = next(r for r in recs.values() if r and r["name"] == "Marco Rossi")
        assert marco["phone"] == "+393331234567" and marco["email"] == ""
        assert set(marco) == set(crm_sync.FIELDS)
        assert "Leoncino" not in str(marco) and "Bologna" not in str(marco)
        assert sum(1 for r in recs.values() if r is None) == 1  # Anna: nothing leaves


def test_csv_is_formula_safe(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, _lead_ = _lead(w, tx, name="=HYPERLINK(1)")
        rows = list(csv.reader(io.StringIO(crm_sync.to_csv(d))))
        assert rows[0] == list(crm_sync.FIELDS[:-1])
        assert rows[1][1].startswith("'=")


def test_sync_needs_a_person_records_ledger_and_erases_after_withdrawal(world):
    from thallo.contracts.actors import AgentActor, HumanOnlyViolation

    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, lead = _lead(w, tx)
        reg = Registry(lambda req: c.CrmSyncResult(synced=len(req.records),
                                                   external_ids={r["thallo_id"]: "101" for r in req.records}),
                       provider="hubspot")
        with pytest.raises(HumanOnlyViolation):
            crm_sync.sync(d, reg, actor=AgentActor(id="a"), verifier=w.verifier)
        out = crm_sync.sync(d, reg, actor=w.human(w.founder), verifier=w.verifier)
        assert out["synced"] == 1 and len(tx.list_all(CrmSyncRecord)) == 1
        assert crm_sync.sync(d, reg, actor=w.human(w.founder), verifier=w.verifier)["status"] == "up_to_date"
        for row in tx.list_all(Consent):
            tx.update(Consent, row.id, withdrawn_at=w.clock.now())
        w.clock.advance(minutes=1)
        crm_sync.sync(d, reg, actor=w.human(w.founder), verifier=w.verifier)
        last = reg.requests[-1].records[0]
        assert last["erase"] is True and last["phone"] == "" and last["external_id"] == "101"
        assert tx.list_all(CrmSyncRecord)[0].erased


def test_manual_floor_records_nothing(world):
    w = world
    with w.backend.tenant(w.tenant.id) as tx:
        d, _lead_ = _lead(w, tx)
        reg = Registry(c.CrmSyncResult(synced=0), status="pending_person", provider="manual", task="Export CSV.")
        out = crm_sync.sync(d, reg, actor=w.human(w.founder), verifier=w.verifier)
        assert out["status"] == "export_csv" and not tx.list_all(CrmSyncRecord)
        assert tx.list_all(Person)
