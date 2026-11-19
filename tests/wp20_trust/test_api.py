"""The trust routes, signed in through the real session flow, on memory and Postgres."""
from __future__ import annotations

from api.context import CSRF_HEADER
from thallo.contracts.models import Variant
from thallo.contracts.states import VariantState
from thallo.trust import service as svc
from thallo.trust.records import ItemOutcome

T = "offer_post"


def test_overview_raise_and_hand_audit(world):
    a, clock = world.a, world.clock
    with world.svc.backend.tenant(a.tenant.id) as tx:
        for n in range(20):
            clock.advance(seconds=1)
            svc.record_item(tx, clock, task_type=T, subject_kind="bundle", subject_id=f"b{n}",
                            outcome=ItemOutcome.CLEAN)
        tx.update(Variant, a.variant_id, state=VariantState.APPROVED)
    c, csrf = world.login(a.founder_email, a.tenant.id)
    body = c.get("/api/trust").json()
    (task,) = body["tasks"]
    assert task["level"] == 0 and not task["can_raise"] and any("Voice" in r for r in task["blocked_by"])
    r = c.post(f"/api/trust/{T}/raise", headers={CSRF_HEADER: csrf})
    assert r.status_code == 422
    assert c.post(f"/api/trust/{T}/raise").status_code in (401, 403)  # no CSRF token

    week = c.get("/api/trust/audit").json()
    assert week["sentences"] and week["sample"] == 20
    sid = week["sentences"][0]["id"]
    r = c.post(f"/api/trust/audit/{sid}", json={"verdict": "correct"}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 200 and r.json()["verdict"] == "correct"
    assert c.get("/api/trust/audit").json()["checked"] == 1

    staff, scsrf = world.login(a.staff_email, a.tenant.id)
    assert staff.post(f"/api/trust/{T}/raise", headers={CSRF_HEADER: scsrf}).status_code == 403


def test_other_tenant_sees_nothing(world):
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        svc.state(tx, world.clock, T)
    c, _ = world.login(world.b.founder_email, world.b.tenant.id)
    assert c.get("/api/trust").json()["tasks"] == []
