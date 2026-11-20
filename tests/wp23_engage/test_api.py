"""The engagement inbox API through the real routes, in memory and on Postgres (migration 0034)."""
from __future__ import annotations

from datetime import timedelta

from api.app import create_app
from api.context import CSRF_HEADER
from tests.wp09_api.conftest import pg_urls, world  # noqa: F401 - fixtures
from thallo.contracts.ids import new_id
from thallo.contracts.models import RegionPolicy
from thallo.leads import Desk


def _setup_desk(w) -> None:
    t, clock = w.a.tenant.id, w.clock
    with w.svc.backend.tenant(t) as tx:
        tx.insert(RegionPolicy(id=new_id(clock), created_at=clock.now(), tenant_id=t, region_code="IT", version=1,
                               rules={"contact": {}}, effective_from=clock.now() - timedelta(days=1)))
        Desk(tx, t, clock).configure("A", service_area=["Bologna"], stock=["Leoncino 500"])


def test_inbox_flow(world):  # noqa: F811
    w = world
    _setup_desk(w)
    c, csrf = w.login(w.a.founder_email, w.a.tenant.id)
    h = {CSRF_HEADER: csrf}
    r = c.post("/api/engage", headers=h, json={"channel": "instagram", "text": "Quanto costa il Leoncino?",
                                               "author": "marco", "permalink": "https://instagram.com/p/1"})
    assert r.status_code == 200, r.text
    lead_item = r.json()
    assert lead_item["category"] == "lead" and lead_item["suggested_reply"]
    r = c.post("/api/engage", headers=h, json={"channel": "facebook", "text": "Vi denuncio, siete truffatori"})
    flagged = r.json()
    assert flagged["state"] == "flagged" and flagged["suggested_reply"] is None
    items = c.get("/api/engage").json()["items"]
    assert items[0]["id"] == flagged["id"]
    assert c.post(f"/api/engage/{flagged['id']}/handled", headers=h).status_code == 409
    r = c.post(f"/api/engage/{flagged['id']}/acknowledge", headers=h)
    assert r.status_code == 200 and r.json()["suggested_reply"], r.text
    r = c.post(f"/api/engage/{flagged['id']}/edit", headers=h, json={"reply": "Ci dispiace, scrivici."})
    assert r.json()["suggested_reply"] == "Ci dispiace, scrivici."
    assert c.post(f"/api/engage/{flagged['id']}/handled", headers=h).json()["state"] == "handled"
    r = c.post(f"/api/engage/{lead_item['id']}/lead", headers=h, json={})
    assert r.status_code == 200, r.text
    lead_id = r.json()["lead_id"]
    assert c.get(f"/api/leads/{lead_id}").status_code == 200
    assert c.get("/api/engage").json()["items"] == []
    assert len(c.get("/api/engage?all=1").json()["items"]) == 2


def test_csrf_tenant_isolation_and_no_send_route(world):  # noqa: F811
    w = world
    c, csrf = w.login(w.a.founder_email, w.a.tenant.id)
    assert c.post("/api/engage", json={"channel": "instagram", "text": "hi"}).status_code in (401, 403)
    item = c.post("/api/engage", headers={CSRF_HEADER: csrf}, json={"channel": "instagram", "text": "hi"}).json()
    cb, csrf_b = w.login(w.b.founder_email, w.b.tenant.id)
    assert cb.get("/api/engage").json()["items"] == []
    assert cb.post(f"/api/engage/{item['id']}/handled", headers={CSRF_HEADER: csrf_b}).status_code == 404
    paths = [getattr(r, "path", "") for r in create_app(w.svc).routes]
    assert not any(p.startswith("/api/engage") and "send" in p for p in paths)
