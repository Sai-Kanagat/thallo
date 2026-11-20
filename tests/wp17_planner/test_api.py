"""The week plan API with the planner: propose, the reasoning in the view, roles, refusal, mirror
through the registry (manual floor), and kill also kills the idea."""
from __future__ import annotations

from api.context import CSRF_HEADER
from thallo.contracts.enums import SourceKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import GrowthModel, Idea, Play, Source
from thallo.contracts.states import IdeaState
from thallo.growth.plays import library
from thallo.planner.records import PlannerSlot

WEEK = "2026-01-12"


def _ready(world):
    clock = world.clock
    spec = library().get("A")
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        t = tx.tenant_id
        play = tx.insert(Play(id=new_id(clock), created_at=clock.now(), tenant_id=t, code="A", name=spec.name,
                              channels=list(spec.channels), formats=list(spec.formats),
                              qualification_questions=[q.model_dump(mode="json")
                                                       for q in spec.qualification_questions],
                              booked_definition=spec.booked_definition,
                              pillar_mix={k: float(v) for k, v in spec.pillar_mix.items()},
                              metrics=list(spec.metrics)))
        gm = tx.list_all(GrowthModel)[0]
        tx.update(GrowthModel, gm.id, play_id=play.id, active=True)
        for i in range(3):
            clock.advance(seconds=1)
            tx.insert(Source(id=new_id(clock), created_at=clock.now(), tenant_id=t, kind=SourceKind.TYPED,
                             title=f"Note {i}", untrusted=True, captured_at=clock.now()))


def test_propose_view_and_mirror(world):
    c, csrf = world.login(world.a.founder_email)
    h = {CSRF_HEADER: csrf}
    r = c.post("/api/plan/propose", json={"week": WEEK}, headers=h)
    assert r.status_code == 409 and "play" in r.text.lower()
    _ready(world)
    r = c.post("/api/plan/propose", json={"week": WEEK}, headers=h)
    assert r.status_code == 200, r.text
    assert c.post("/api/plan/propose", json={"week": WEEK}, headers=h).status_code == 409
    body = c.get(f"/api/plan?week={WEEK}").json()
    assert len(body["planner"]["reasoning"]) == 2 and body["planner"]["not_doing"]
    planned = [s["planned"] for s in body["slots"]]
    assert planned and all(p and p["play"] == "A" and p["target_metric"] and p["reason"] for p in planned)
    assert all(p["forecast"] is None and p["no_forecast_reason"] for p in planned)
    r = c.post("/api/plan/mirror", json={"week": WEEK}, headers=h)
    assert r.status_code == 200 and r.json()["provider"] == "manual" and r.json()["mirrored"] == 0
    assert c.get(f"/api/plan?week={WEEK}").json()["mirror"]["provider"] == "manual"
    slot_id = body["slots"][0]["id"]
    assert c.post(f"/api/plan/slots/{slot_id}/kill", headers=h).status_code == 200
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        p = tx.list_all(PlannerSlot, slot_id=slot_id)[0]
        assert tx.get(Idea, p.idea_id).state is IdeaState.KILLED


def test_staff_cannot_propose(world):
    c, csrf = world.login(world.a.staff_email)
    assert c.post("/api/plan/propose", json={"week": WEEK}, headers={CSRF_HEADER: csrf}).status_code == 403
    assert c.post("/api/plan/mirror", json={"week": WEEK}, headers={CSRF_HEADER: csrf}).status_code == 403
