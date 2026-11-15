"""Week plan and connections through the API: roles, isolation, hostile settings, secrets that
never come back out."""
from __future__ import annotations

import logging

from api.context import CSRF_HEADER
from thallo.contracts.models import PlanSlot
from thallo.contracts.states import PlanSlotState

KEY = "sk-test-DO-NOT-LEAK-0123456789abcdef"


# ---------------------------------------------------------------------- plan

def test_week_plan_shows_slots_and_stage_mix(world):
    c, _ = world.login(world.a.founder_email)
    r = c.get("/api/plan?week=2026-01-07")  # any day of the week resolves to its Monday
    assert r.status_code == 200
    body = r.json()
    assert body["week_start"] == "2026-01-05" and len(body["days"]) == 7
    assert [s["id"] for s in body["slots"]] == world.a.slot_ids
    mix = {m["stage"]: m for m in body["stage_mix"]}
    assert mix["consideration"]["share"] == 1.0 and mix["consideration"]["target"] == 0.6
    assert mix["awareness"]["target"] == 0.4


def test_kill_and_move(world):
    c, csrf = world.login(world.a.founder_email)
    open_id, other_id, missed_id = world.a.slot_ids
    assert c.post(f"/api/plan/slots/{open_id}/kill", headers={CSRF_HEADER: csrf}).status_code == 200
    assert c.post(f"/api/plan/slots/{open_id}/kill", headers={CSRF_HEADER: csrf}).status_code == 409
    assert c.post(f"/api/plan/slots/{missed_id}/kill", headers={CSRF_HEADER: csrf}).status_code == 200
    r = c.post(f"/api/plan/slots/{other_id}/move", json={"starts_at": "2026-01-09T10:00:00+00:00"},
               headers={CSRF_HEADER: csrf})
    assert r.status_code == 200
    r = c.post(f"/api/plan/slots/{other_id}/move", json={"starts_at": "2026-01-20T10:00:00+00:00"},
               headers={CSRF_HEADER: csrf})
    assert r.status_code == 400
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        assert tx.get(PlanSlot, open_id).state is PlanSlotState.KILLED


def test_plan_needs_csrf_and_role(world):
    c, _ = world.login(world.a.founder_email)
    assert c.post(f"/api/plan/slots/{world.a.slot_ids[0]}/kill").status_code == 403
    s, s_csrf = world.login(world.a.staff_email)
    assert s.get("/api/plan").status_code == 403
    assert s.post(f"/api/plan/slots/{world.a.slot_ids[0]}/kill", headers={CSRF_HEADER: s_csrf}).status_code == 403


def test_plan_cross_tenant_not_found(world):
    c, csrf = world.login(world.a.founder_email)
    slot = world.b.slot_ids[0]
    assert c.post(f"/api/plan/slots/{slot}/kill", headers={CSRF_HEADER: csrf}).status_code == 404
    assert c.post(f"/api/plan/slots/{slot}/move", json={"starts_at": "2026-01-06T10:00:00+00:00"},
                  headers={CSRF_HEADER: csrf}).status_code == 404
    assert world.b.slot_ids[0] not in [s["id"] for s in c.get("/api/plan?week=2026-01-05").json()["slots"]]


# ---------------------------------------------------------------------- connections

def _add(c, csrf, **body):
    return c.post("/api/connections", json=body, headers={CSRF_HEADER: csrf})


def test_list_shows_manual_floor_for_every_capability(world):
    c, _ = world.login(world.a.founder_email)
    caps = c.get("/api/connections").json()["capabilities"]
    assert len(caps) == 16
    assert all(any(x["is_floor"] for x in cap["connections"]) for cap in caps)
    offered = {(p["capability"], p["provider"]) for p in c.get("/api/connections/providers").json()["providers"]}
    assert ("text", "claude") in offered and ("text", "openai_compatible") not in offered


def test_add_claude_secret_never_comes_back(world, caplog):
    caplog.set_level(logging.DEBUG)
    c, csrf = world.login(world.a.founder_email)
    r = _add(c, csrf, capability="text", provider="claude", model="claude-opus-5-5", role="draft",
             settings={"input_usd_per_mtok": "5", "output_usd_per_mtok": "25", "effort": "high"}, secret=KEY)
    assert r.status_code == 201, r.text
    conn = r.json()["connection"]
    assert conn["has_secret"] is True and conn["fixed_origin"] == "https://api.anthropic.com"
    texts = [r.text, c.get("/api/connections").text, c.get("/api/connections/providers").text]
    r2 = c.post(f"/api/connections/{conn['id']}/secret", json={"secret": KEY + "x"}, headers={CSRF_HEADER: csrf})
    texts.append(r2.text)
    assert r2.status_code == 200
    # a refused add with the key in it
    texts.append(_add(c, csrf, capability="text", provider="claude", settings={"base_url": "https://evil.test"},
                      secret=KEY).text)
    texts.append(_add(c, csrf, capability="text", provider="claude", secret=KEY + "\n").text)
    texts.append(c.post("/api/connections", json={"capability": "text", "provider": "claude", "secret": KEY,
                                                  "bogus": KEY}, headers={CSRF_HEADER: csrf}).text)
    for t in texts:
        assert KEY not in t
    assert KEY not in caplog.text


def test_hostile_settings_rejected(world):
    c, csrf = world.login(world.a.founder_email)
    hostile = [
        {"capability": "text", "provider": "openai_compatible", "settings": {"base_url": "https://evil.test/v1"}},
        {"capability": "text", "provider": "claude", "settings": {"base_url": "https://evil.test"}},
        {"capability": "image", "provider": "template_render", "settings": {"chromium": "/usr/bin/evil"}},
        {"capability": "image", "provider": "template_render", "settings": {"language": "../../etc"}},
        {"capability": "text", "provider": "claude", "settings": {"effort": "http://evil.test"}},
        {"capability": "text", "provider": "local", "settings": {"timeout_s": {"nested": 1}}},
        {"capability": "text", "provider": "claude", "model": "../../bin/sh"},
    ]
    for body in hostile:
        r = _add(c, csrf, **body)
        assert r.status_code == 400, (body, r.text)
        assert "evil" not in r.text and "/usr/bin" not in r.text
    assert all(x["provider"] == "manual" for cap in c.get("/api/connections").json()["capabilities"]
               for x in cap["connections"])


def test_enable_order_test_remove(world):
    c, csrf = world.login(world.a.founder_email)
    r = _add(c, csrf, capability="crm", provider="mock")
    assert r.status_code == 201, r.text
    cid = r.json()["connection"]["id"]
    r = c.post(f"/api/connections/{cid}/test", headers={CSRF_HEADER: csrf})
    assert r.status_code == 200 and r.json()["health"] == "connected"
    listed = next(cap for cap in c.get("/api/connections").json()["capabilities"] if cap["capability"] == "crm")
    assert next(x for x in listed["connections"] if x["id"] == cid)["health"] == "connected"
    assert c.post(f"/api/connections/{cid}/enabled", json={"enabled": False},
                  headers={CSRF_HEADER: csrf}).json()["connection"]["enabled"] is False
    floor = next(x for x in listed["connections"] if x["is_floor"])
    assert c.post(f"/api/connections/{floor['id']}/enabled", json={"enabled": False},
                  headers={CSRF_HEADER: csrf}).status_code == 409
    assert c.delete(f"/api/connections/{floor['id']}", headers={CSRF_HEADER: csrf}).status_code == 409
    assert c.delete(f"/api/connections/{cid}", headers={CSRF_HEADER: csrf}).status_code == 200


def test_connections_cross_tenant_and_roles(world):
    b, b_csrf = world.login(world.b.founder_email)
    cid = _add(b, b_csrf, capability="crm", provider="mock").json()["connection"]["id"]
    a, csrf = world.login(world.a.founder_email)
    assert cid not in a.get("/api/connections").text
    for method, path, body in (("post", f"/api/connections/{cid}/test", None),
                               ("post", f"/api/connections/{cid}/enabled", {"enabled": False}),
                               ("post", f"/api/connections/{cid}/secret", {"secret": "abc"}),
                               ("post", "/api/connections/order", {"ids": [cid]}),
                               ("delete", f"/api/connections/{cid}", None)):
        r = getattr(a, method)(path, **({"json": body} if body else {}), headers={CSRF_HEADER: csrf})
        assert r.status_code == 404, (path, r.status_code)
    assert a.post("/api/connections", json={"capability": "crm", "provider": "mock"}).status_code == 403  # no CSRF
    s, s_csrf = world.login(world.a.staff_email)
    assert s.get("/api/connections").status_code == 403
    assert _add(s, s_csrf, capability="crm", provider="mock").status_code == 403
