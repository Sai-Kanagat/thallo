"""Growth API on memory and Postgres: roles, tags set by the server, policy guard fails closed,
tenant isolation."""
from __future__ import annotations

from api.context import CSRF_HEADER
from thallo.contracts.ids import new_id
from thallo.contracts.models import GrowthModel, Play, RegionPolicy

ANSWERS = {"play_code": "A", "margin_per_won": "600",
           "rates": {"booked_to_held": {"rate": "0.70"}, "held_to_won": {"rate": "0.25"},
                     "enquiry_to_booked": {"rate": "0.2", "low": "0.1", "high": "0.3"}}}


def _policy(world, tenant, rules):  # noqa: ANN001, ANN202
    clock = world.svc.clock
    with world.svc.backend.tenant(tenant.id) as tx:
        tx.insert(RegionPolicy(id=new_id(clock), tenant_id=tenant.id, created_at=clock.now(),
                               region_code=tenant.region_code, version=1, rules=rules,
                               effective_from=clock.now()))


def test_get_labels_every_number(world):
    c, _ = world.login(world.a.founder_email)
    r = c.get("/api/growth")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] is None and body["measured_after"] == 30
    assert [p["code"] for p in body["plays"]] == ["A", "B", "C"]
    for p in body["plays"]:
        assert p["evidence"] == "guess" and "Not a benchmark" in p["evidence_label"]
        assert p["policy"]["status"] == "no_policy"


def test_model_save_needs_a_policy_for_its_play(world):
    c, csrf = world.login(world.a.founder_email)
    r = c.post("/api/growth/model", json=ANSWERS, headers={CSRF_HEADER: csrf})
    assert r.status_code == 422 and "No region policy" in r.json()["message"]


def test_save_model_is_guessed_and_versions(world):
    _policy(world, world.a.tenant, {})
    c, csrf = world.login(world.a.founder_email)
    r = c.post("/api/growth/model", json=ANSWERS, headers={CSRF_HEADER: csrf})
    assert r.status_code == 200, r.text
    m = r.json()["model"]
    assert m["summary"]["allowable_cost"] == "31.50" and m["summary"]["tag"] == "guessed"
    assert m["summary"]["currency"] == "EUR" and m["play_code"] == "A"
    assert {s["tag"] for s in m["stages"]} == {"guessed"}
    assert "not measured" in m["bottleneck"]
    r2 = c.post("/api/growth/model", json={**ANSWERS, "share_of_margin": "0.2"}, headers={CSRF_HEADER: csrf})
    assert r2.json()["model"]["version"] == m["version"] + 1
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        assert sum(1 for g in tx.list_all(GrowthModel) if g.active) == 1
    assert c.get("/api/growth").json()["model"]["share_of_margin"] == "0.2"


def test_model_refuses_a_client_tag(world):
    c, csrf = world.login(world.a.founder_email)
    bad = {**ANSWERS, "rates": {"booked_to_held": {"rate": "0.7", "tag": "measured"}, "held_to_won": {"rate": "0.25"}}}
    assert c.post("/api/growth/model", json=bad, headers={CSRF_HEADER: csrf}).status_code == 422


def test_preview_writes_nothing(world):
    c, csrf = world.login(world.a.founder_email)
    r = c.post("/api/growth/preview", json=ANSWERS, headers={CSRF_HEADER: csrf})
    assert r.status_code == 200 and r.json()["summary"]["allowable_cost"] == "31.50"
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        assert not tx.list_all(Play)


def test_staff_cannot_edit(world):
    c, csrf = world.login(world.a.staff_email)
    assert c.post("/api/growth/model", json=ANSWERS, headers={CSRF_HEADER: csrf}).status_code == 403
    assert c.post("/api/growth/plays/A", json={}, headers={CSRF_HEADER: csrf}).status_code == 403


def test_play_save_needs_a_policy(world):
    c, csrf = world.login(world.a.founder_email)
    r = c.post("/api/growth/plays/A", json={"name": "Dealer play"}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 422 and "No region policy" in r.json()["message"]


def test_play_save_checks_the_policy(world):
    _policy(world, world.a.tenant, {"blocked_channels": ["social_ads"]})
    c, csrf = world.login(world.a.founder_email)
    r = c.post("/api/growth/plays/A", json={}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 422 and "social_ads" in r.json()["message"]
    channels = ["google_business_profile", "whatsapp", "facebook", "instagram", "search_ads"]
    r = c.post("/api/growth/plays/A", json={"channels": channels}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 200, r.text
    play = next(p for p in c.get("/api/growth").json()["plays"] if p["code"] == "A")
    assert play["saved"] and play["policy"]["status"] == "ok" and "social_ads" not in play["channels"]


def test_play_edit_is_validated(world):
    _policy(world, world.a.tenant, {})
    c, csrf = world.login(world.a.founder_email)
    h = {CSRF_HEADER: csrf}
    r = c.post("/api/growth/plays/B", json={"pillar_mix": {"a_one": "0.6", "b_two": "0.3"}}, headers=h)
    assert r.status_code == 422 and "sum to exactly 1" in r.json()["message"]
    r = c.post("/api/growth/plays/B", json={"metrics": ["qualified_leads"]}, headers=h)
    assert r.status_code == 422 and "headline_metric" in r.json()["message"]
    r = c.post("/api/growth/plays/B-reseller", json={"name": "Reseller trial"}, headers=h)
    assert r.status_code == 200
    r = c.post("/api/growth/plays/Z", json={}, headers=h)
    assert r.status_code == 422
    codes = [p["code"] for p in c.get("/api/growth").json()["plays"]]
    assert codes == ["A", "B", "C", "B-reseller"]


def test_tenants_are_isolated(world):
    _policy(world, world.a.tenant, {})
    ca, csrf = world.login(world.a.founder_email)
    ca.post("/api/growth/model", json=ANSWERS, headers={CSRF_HEADER: csrf})
    cb, _ = world.login(world.b.founder_email)
    body = cb.get("/api/growth").json()
    assert body["model"] is None and not any(p["saved"] for p in body["plays"])
