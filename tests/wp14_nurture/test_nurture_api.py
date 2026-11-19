"""The WP-14 routes end to end, in memory and on Postgres (migration 0031)."""
from __future__ import annotations

from datetime import timedelta

from api.context import CSRF_HEADER
from tests.wp09_api.conftest import pg_urls, world  # noqa: F401 - fixtures
from thallo.contracts.actors import HumanActor
from thallo.contracts.enums import ConsentBasis, MessageChannel, TouchKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import RegionPolicy
from thallo.leads import Desk, TouchEvent

RULES = {"contact": {"whatsapp": {"bases": ["enquiry"], "footer": "Rispondi STOP per non ricevere altri messaggi"}}}
BODY = "Ciao {{name}}, ti va di passare a vedere il {{model}}? Siamo in negozio e ti aspettiamo."
ANSWERS = {"model": "Leoncino 500", "payment": "cash", "town": "Bologna", "timing": "this_week"}


class _Yes:
    def is_live_human_session(self, actor: HumanActor) -> bool:
        return True


def _seed(w) -> str:
    shop, clock = w.a, w.clock
    t = shop.tenant.id
    with w.svc.backend.tenant(t) as tx:
        tx.insert(RegionPolicy(id=new_id(clock), created_at=clock.now(), tenant_id=t, region_code="IT", version=1,
                               rules=RULES, effective_from=clock.now() - timedelta(days=1)))
        d = Desk(tx, t, clock)
        d.configure("A", service_area=["Bologna"], stock=["Leoncino 500"])
        founder = HumanActor(id=shop.founder.id, tenant_id=t, session_id="seed")
        ev = TouchEvent(kind=TouchKind.FORM, occurred_at=clock.now(), display_name="Marco", phone="+39 333 123 4567")
        person = d._person(ev)
        d.record_consent(person.id, channel=MessageChannel.WHATSAPP, recipient="+393331234567",
                         basis=ConsentBasis.ENQUIRY, evidence="form opt in", actor=founder, verifier=_Yes())
        lead = d.intake(ev)
        return d.record_answers(lead.id, ANSWERS).id


def test_nurture_booking_pipeline_and_crm_routes(world):  # noqa: F811
    w = world
    lead_id = _seed(w)
    c, csrf = w.login(w.a.founder_email, w.a.tenant.id)
    h = {CSRF_HEADER: csrf}
    r = c.post("/api/nurture/templates", headers=h, json={
        "name": "n1", "channel": "whatsapp", "language": "it", "body": BODY,
        "slot_sources": {"name": "person.display_name", "model": "answer.model"}})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert c.post(f"/api/nurture/templates/{tid}/approve", headers=h).status_code == 200
    r = c.post("/api/nurture/sequences", headers=h, json={
        "name": "warm", "channel": "whatsapp", "steps": [{"template_id": tid, "wait_hours": 0}],
        "quiet_start_hour": 22, "quiet_end_hour": 6})
    sid = r.json()["id"]
    assert c.post(f"/api/nurture/leads/{lead_id}/enrol", headers=h, json={"sequence_id": sid}).status_code == 200
    assert c.post("/api/nurture/run", headers=h).json()["drafted"] == 1
    msg = next(m for m in c.get(f"/api/leads/{lead_id}").json()["messages"] if m["kind"] == "nurture")
    assert not msg["editable"]
    plain = c.post(f"/api/leads/messages/{msg['id']}/send", headers=h, json={"body_sha256": msg["body_sha256"]})
    assert plain.status_code == 409  # the plain lead desk route does not carry nurture
    r = c.post(f"/api/nurture/messages/{msg['id']}/send", headers=h, json={"body_sha256": msg["body_sha256"]})
    assert r.status_code == 200 and r.json()["status"] == "open_in_app", r.text
    r = c.post(f"/api/nurture/leads/{lead_id}/booking", headers=h,
               json={"kind": "test_drive", "starts_at": (w.clock.now() + timedelta(days=1)).isoformat()})
    assert r.status_code == 200, r.text
    board = c.get("/api/nurture/pipeline").json()
    assert board["booked_conversations"] == 1
    csv_text = c.get("/api/nurture/crm/export.csv").text
    assert csv_text.startswith("thallo_id,name,email,phone") and "+393331234567" in csv_text
    assert c.post("/api/nurture/crm/sync", headers=h).json()["status"] == "export_csv"


def test_a_refused_tap_after_a_reply_discards_the_draft(world):  # noqa: F811
    w = world
    lead_id = _seed(w)
    c, csrf = w.login(w.a.founder_email, w.a.tenant.id)
    h = {CSRF_HEADER: csrf}
    tid = c.post("/api/nurture/templates", headers=h, json={
        "name": "n1", "channel": "whatsapp", "language": "it", "body": BODY,
        "slot_sources": {"name": "person.display_name", "model": "answer.model"}}).json()["id"]
    c.post(f"/api/nurture/templates/{tid}/approve", headers=h)
    sid = c.post("/api/nurture/sequences", headers=h, json={
        "name": "warm", "channel": "whatsapp", "steps": [{"template_id": tid, "wait_hours": 0}],
        "quiet_start_hour": 22, "quiet_end_hour": 6}).json()["id"]
    c.post(f"/api/nurture/leads/{lead_id}/enrol", headers=h, json={"sequence_id": sid})
    c.post("/api/nurture/run", headers=h)
    msg = next(m for m in c.get(f"/api/leads/{lead_id}").json()["messages"] if m["kind"] == "nurture")
    w.clock.advance(minutes=5)
    with w.svc.backend.tenant(w.a.tenant.id) as tx:
        Desk(tx, w.a.tenant.id, w.clock).intake(TouchEvent(kind=TouchKind.WHATSAPP_CODE, occurred_at=w.clock.now(),
                                                           display_name="Marco", phone="+39 333 123 4567"))
    r = c.post(f"/api/nurture/messages/{msg['id']}/send", headers=h, json={"body_sha256": msg["body_sha256"]})
    assert r.status_code == 409
    after = next(m for m in c.get(f"/api/leads/{lead_id}").json()["messages"] if m["id"] == msg["id"])
    assert after["state"] == "discarded"


def test_roles_and_csrf(world):  # noqa: F811
    w = world
    _seed(w)
    staff, csrf = w.login(w.a.staff_email, w.a.tenant.id)
    assert staff.get("/api/nurture/crm/export.csv").status_code == 403
    assert staff.post("/api/nurture/crm/sync", headers={CSRF_HEADER: csrf}).status_code == 403
    c, _ = w.login(w.a.founder_email, w.a.tenant.id)
    assert c.post("/api/nurture/run").status_code == 403  # no CSRF
