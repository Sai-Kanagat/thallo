"""Review deck through the API: the card, approve with flags, edit voids, reject, media, isolation,
and the real SessionVerifier (only an interactive, signed-in session is human)."""
from __future__ import annotations

import hashlib
from dataclasses import replace

from api.context import COOKIE, CSRF_HEADER
from api.sessions import SessionKind
from thallo.contracts.actors import HumanActor
from thallo.contracts.models import Approval, Bundle, Edit, Media, Variant
from thallo.contracts.states import BundleState, VariantState


def _card(c, world, shop=None):
    shop = shop or world.a
    r = c.get(f"/api/review/bundles/{shop.bundle_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _approve(c, csrf, world, ack, seconds=30, shop=None):
    shop = shop or world.a
    return c.post(f"/api/review/bundles/{shop.bundle_id}/approve",
                  json={"acknowledged_flag_ids": ack, "seconds_on_screen": seconds}, headers={CSRF_HEADER: csrf})


def test_card_shows_hash_text_flags_and_no_blockers(world):
    c, _ = world.login(world.a.founder_email)
    listing = c.get("/api/review/bundles").json()["bundles"]
    assert [b["bundle_id"] for b in listing] == [world.a.bundle_id]
    card = _card(c, world)
    assert len(card["root_hash"]) == 64
    assert [f["id"] for f in card["flags"]] == world.a.flag_ids
    assert card["blocking"] == []
    assert card["can_approve"] is True
    v = card["variants"][0]
    assert "120" in v["text"] and "{{C-0001}}" in v["source_text"]
    assert v["media"][0]["url"] == f"/api/media/{world.a.media_id}"
    assert card["why"] == "Range is the top question"


def test_unacknowledged_flag_refused_acknowledged_granted(world):
    c, csrf = world.login(world.a.founder_email)
    _card(c, world)
    world.clock.advance(seconds=40)
    r = _approve(c, csrf, world, [])
    assert r.status_code == 409 and "not acknowledged" in r.json()["message"]
    r = _approve(c, csrf, world, world.a.flag_ids, seconds=500)
    assert r.status_code == 200, r.text
    assert r.json()["seconds_on_screen"] == 40.0  # clamped to what the server saw
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        (approval,) = tx.list_all(Approval)
        assert approval.acknowledged_flag_ids == world.a.flag_ids
        assert tx.get(Bundle, world.a.bundle_id).state is BundleState.APPROVED
        assert approval.approver_id == world.a.founder.id


def test_approve_without_showing_the_card_is_refused(world):
    c, csrf = world.login(world.a.founder_email)
    r = _approve(c, csrf, world, world.a.flag_ids)
    assert r.status_code == 409 and "open the card" in r.json()["message"]


def test_client_cannot_send_a_hash_or_tenant(world):
    c, csrf = world.login(world.a.founder_email)
    _card(c, world)
    r = c.post(f"/api/review/bundles/{world.a.bundle_id}/approve",
               json={"acknowledged_flag_ids": world.a.flag_ids, "seconds_on_screen": 1,
                     "shown_hash": "0" * 64, "tenant_id": world.b.tenant.id}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 422  # unknown fields are refused outright


def test_content_changed_after_card_shown_is_refused(world):
    c, csrf = world.login(world.a.founder_email)
    _card(c, world)
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        v = tx.get(Variant, world.a.variant_id)
        tx.update(Variant, v.id, rendered_text=v.rendered_text + " Nuovo.")
    r = _approve(c, csrf, world, world.a.flag_ids)
    assert r.status_code == 409


def test_csrf_missing_on_approve_is_refused(world):
    c, _ = world.login(world.a.founder_email)
    _card(c, world)
    r = c.post(f"/api/review/bundles/{world.a.bundle_id}/approve",
               json={"acknowledged_flag_ids": world.a.flag_ids, "seconds_on_screen": 1})
    assert r.status_code == 403 and r.json()["error"] == "csrf"


def test_staff_cannot_review(world):
    c, csrf = world.login(world.a.staff_email)
    assert c.get("/api/review/bundles").status_code == 403
    assert c.get(f"/api/review/bundles/{world.a.bundle_id}").status_code == 403
    assert c.get(f"/api/media/{world.a.media_id}").status_code == 403


def test_non_interactive_session_cannot_approve(world):
    """A service or API-token session bound to the founder's membership can read, never approve."""
    for kind in (SessionKind.SERVICE, SessionKind.API_TOKEN):
        token, s = world.svc.sessions.create(kind)
        s = replace(s, authenticated=True, identity_id=world.a.founder.identity_id, tenant_id=world.a.tenant.id,
                    app_user_id=world.a.founder.id)
        world.svc.sessions.save(s)
        c = world.client()
        c.cookies.set(COOKIE, token)
        assert c.get(f"/api/review/bundles/{world.a.bundle_id}").status_code == 200
        r = _approve(c, s.csrf, world, world.a.flag_ids)
        assert r.status_code == 403 and r.json()["error"] == "not_human"
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        assert tx.list_all(Approval) == []


def test_verifier_refuses_everything_but_the_live_interactive_session(world):
    c, _ = world.login(world.a.founder_email)
    v = world.svc.verifier
    live = next(s for s in world.svc.sessions._by_hash.values() if s.app_user_id == world.a.founder.id)
    good = HumanActor(id=world.a.founder.id, tenant_id=world.a.tenant.id, session_id=live.id)
    assert v.is_live_human_session(good)
    assert not v.is_live_human_session(good.model_copy(update={"tenant_id": world.b.tenant.id}))
    assert not v.is_live_human_session(good.model_copy(update={"id": world.a.staff.id}))
    assert not v.is_live_human_session(good.model_copy(update={"session_id": "made-up"}))
    world.svc.sessions.save(replace(live, kind=SessionKind.SERVICE))
    assert not v.is_live_human_session(good)
    world.svc.sessions.save(replace(live, kind=SessionKind.INTERACTIVE, authenticated=False))
    assert not v.is_live_human_session(good)


def test_edit_voids_approval_first_and_resets_checks(world):
    c, csrf = world.login(world.a.founder_email)
    _card(c, world)
    assert _approve(c, csrf, world, world.a.flag_ids).status_code == 200
    r = c.post(f"/api/review/variants/{world.a.variant_id}/edit",
               json={"text": "Fino a {{C-0001}} con una carica.", "kind": "cut"}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 200, r.text
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        (approval,) = tx.list_all(Approval)
        assert approval.voided_at is not None and approval.void_reason == "edited in review"
        v = tx.get(Variant, world.a.variant_id)
        assert v.state is VariantState.DRAFTING and v.rendered_text is None and v.version == 2
        assert tx.get(Bundle, world.a.bundle_id).state is BundleState.OPEN
        (edit,) = tx.list_all(Edit)
        assert edit.editor_id == world.a.founder.id and edit.after_text.endswith("carica.")
        voids = [e for e in tx.audit.store.entries(world.a.tenant.id) if e.action == "approval.voided"]
        assert len(voids) == 1


def test_edit_without_approval_and_old_checks_stop_counting(world):
    c, csrf = world.login(world.a.founder_email)
    r = c.post(f"/api/review/variants/{world.a.variant_id}/edit", json={"text": "Nuovo testo."},
               headers={CSRF_HEADER: csrf})
    assert r.status_code == 200
    card = _card(c, world)
    assert any(b["kind"] == "not_checked" for b in card["blocking"])
    assert card["can_approve"] is False


def test_reject_with_reason(world):
    c, csrf = world.login(world.a.founder_email)
    r = c.post(f"/api/review/bundles/{world.a.bundle_id}/reject", json={"reason": "off_voice"},
               headers={CSRF_HEADER: csrf})
    assert r.status_code == 200
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        assert tx.get(Bundle, world.a.bundle_id).state is BundleState.REJECTED
        assert tx.get(Variant, world.a.variant_id).state is VariantState.REJECTED
        assert tx.list_all(Edit)[0].rejection_reason.value == "off_voice"


def test_withdraw_voids(world):
    c, csrf = world.login(world.a.founder_email)
    _card(c, world)
    assert _approve(c, csrf, world, world.a.flag_ids).status_code == 200
    r = c.post(f"/api/review/bundles/{world.a.bundle_id}/withdraw", headers={CSRF_HEADER: csrf})
    assert r.status_code == 200 and r.json()["state"] == "voided"


def test_cross_tenant_review_routes_are_not_found(world):
    c, csrf = world.login(world.a.founder_email)
    b = world.b
    assert c.get(f"/api/review/bundles/{b.bundle_id}").status_code == 404
    assert c.get(f"/api/media/{b.media_id}").status_code == 404
    for path, body in ((f"/api/review/bundles/{b.bundle_id}/approve", {"seconds_on_screen": 1}),
                       (f"/api/review/bundles/{b.bundle_id}/reject", {"reason": "other"}),
                       (f"/api/review/bundles/{b.bundle_id}/withdraw", None),
                       (f"/api/review/variants/{b.variant_id}/edit", {"text": "x"})):
        r = c.post(path, json=body, headers={CSRF_HEADER: csrf})
        assert r.status_code in (404, 409), (path, r.status_code)
        assert r.status_code == 404 or "open the card" in r.json()["message"]
    with world.svc.backend.tenant(b.tenant.id) as tx:
        assert tx.get(Bundle, b.bundle_id).state is BundleState.IN_REVIEW
        assert tx.get(Variant, b.variant_id).text.startswith("Fino a")


def test_member_of_both_tenants_sees_only_the_selected_one(world):
    c, csrf = world.login(world.both_email, tenant_id=world.a.tenant.id)
    assert c.get(f"/api/review/bundles/{world.a.bundle_id}").status_code == 200
    assert c.get(f"/api/review/bundles/{world.b.bundle_id}").status_code == 404
    _card(c, world)
    # switching tenant drops what was shown, so a card seen in A cannot be approved from B
    r = c.post("/api/auth/tenant", json={"tenant_id": world.b.tenant.id}, headers={CSRF_HEADER: csrf})
    csrf = r.json()["csrf_token"]
    assert _approve(c, csrf, world, world.a.flag_ids).status_code in (404, 409)


def test_media_served_with_hash_check_and_strict_headers(world):
    c, _ = world.login(world.a.founder_email)
    r = c.get(f"/api/media/{world.a.media_id}")
    assert r.status_code == 200
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(b"\x89PNG review deck image").hexdigest()
    assert r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers["content-security-policy"]


def test_media_row_pointing_into_another_tenant_folder_is_refused(world):
    c, _ = world.login(world.a.founder_email)
    b_file = world.media_root / world.b.tenant.id.lower() / "hero.png"
    hostile = [f"local:{world.b.tenant.id.lower()}/hero.png", b_file.as_uri(),
               (world.media_root / world.a.tenant.id.lower() / ".." / world.b.tenant.id.lower() / "hero.png").as_uri(),
               "file:///etc/hosts"]
    for uri in hostile:
        with world.svc.backend.tenant(world.a.tenant.id) as tx:
            tx.update(Media, world.a.media_id, storage_uri=uri)
        assert c.get(f"/api/media/{world.a.media_id}").status_code == 404, uri


def test_media_through_a_symlink_is_refused(world):
    c, _ = world.login(world.a.founder_email)
    a_dir = world.media_root / world.a.tenant.id.lower()
    link = a_dir / "sneaky.png"
    link.symlink_to(world.media_root / world.b.tenant.id.lower() / "hero.png")
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        tx.update(Media, world.a.media_id, storage_uri=f"local:{world.a.tenant.id.lower()}/sneaky.png")
    assert c.get(f"/api/media/{world.a.media_id}").status_code == 404


def test_media_bytes_changed_are_not_served(world):
    c, _ = world.login(world.a.founder_email)
    (world.media_root / world.a.tenant.id.lower() / "hero.png").write_bytes(b"swapped")
    assert c.get(f"/api/media/{world.a.media_id}").status_code == 409
    card = _card(c, world)
    assert card["can_approve"] is False  # the bytes no longer match their check rows
