"""The lead desk API through the real routes, in memory and on Postgres (migration 0027)."""
from __future__ import annotations

from datetime import timedelta

from api.context import CSRF_HEADER
from tests.wp09_api.conftest import pg_urls, world  # noqa: F401 - fixtures
from thallo.contracts.actors import HumanActor
from thallo.contracts.enums import ConsentBasis, Language, MessageChannel, TouchKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import RegionPolicy
from thallo.leads import Desk, TouchEvent

ACK = "Ciao {{name}}, abbiamo ricevuto il tuo messaggio. Ti risponde una persona del negozio."
RULES = {"contact": {"whatsapp": {"bases": ["enquiry"], "footer": "Rispondi STOP per non ricevere altri messaggi"}}}


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
        tpl = d.new_template(name="ack", channel=MessageChannel.WHATSAPP, language=Language.IT, body=ACK,
                             slot_sources={"name": "person.display_name"})
        d.approve_template(tpl.id, actor=founder, verifier=_Yes())
        ev = TouchEvent(kind=TouchKind.WHATSAPP_CODE, occurred_at=clock.now(), display_name="Marco",
                        phone="+39 333 123 4567")
        person = d._person(ev)
        d.record_consent(person.id, channel=MessageChannel.WHATSAPP, recipient="+393331234567",
                         basis=ConsentBasis.ENQUIRY, evidence="started the chat", actor=founder, verifier=_Yes())
        return d.intake(ev).id


def test_lead_desk_flow(world):  # noqa: F811
    w = world
    lead_id = _seed(w)
    c, csrf = w.login(w.a.founder_email, w.a.tenant.id)
    grid = c.get("/api/leads").json()
    assert grid["set_up"] and grid["leads"][0]["id"] == lead_id
    row = grid["leads"][0]
    assert row["may_message"] and row["clock"]["running"] and isinstance(row["fit"], int)
    detail = c.get(f"/api/leads/{lead_id}").json()
    (ack,) = detail["messages"]
    assert ack["kind"] == "acknowledgement" and not ack["editable"]
    r = c.post(f"/api/leads/{lead_id}/answers", headers={CSRF_HEADER: csrf},
               json={"answers": {"model": "Leoncino 500", "payment": "cash", "town": "Bologna",
                                 "timing": "this_week"}})
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["route"] == "hot" and detail["fit"] == 100
    reply = next(m for m in detail["messages"] if m["kind"] == "reply_draft")
    r = c.post(f"/api/leads/messages/{reply['id']}/send", headers={CSRF_HEADER: csrf},
               json={"body_sha256": "0" * 64})
    assert r.status_code == 409 and r.json()["error"] == "changed"
    r = c.post(f"/api/leads/messages/{reply['id']}/send", headers={CSRF_HEADER: csrf},
               json={"body_sha256": reply["body_sha256"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "open_in_app" and out["link"].startswith("https://wa.me/393331234567?text=")
    r = c.post(f"/api/leads/messages/{reply['id']}/sent", headers={CSRF_HEADER: csrf})
    assert r.status_code == 200, r.text
    detail = c.get(f"/api/leads/{lead_id}").json()
    assert detail["state"] == "contacted" and not detail["clock"]["running"]
    r = c.post(f"/api/leads/{lead_id}/outcome", headers={CSRF_HEADER: csrf},
               json={"kind": "lost", "reason": "timing"})
    assert r.status_code == 200 and r.json()["state"] == "lost"


def test_role_csrf_and_tenant_isolation(world):  # noqa: F811
    w = world
    lead_id = _seed(w)
    staff, _ = w.login(w.a.staff_email, w.a.tenant.id)
    assert staff.get("/api/leads").status_code == 403
    other, csrf_b = w.login(w.b.founder_email, w.b.tenant.id)
    assert other.get("/api/leads").json() == {"set_up": False, "leads": []}
    assert other.get(f"/api/leads/{lead_id}").status_code == 404
    c, _csrf = w.login(w.a.founder_email, w.a.tenant.id)
    assert c.post(f"/api/leads/{lead_id}/answers", json={"answers": {}}).status_code == 403  # no CSRF
    assert c.get("/api/leads/not-an-id").status_code == 404
