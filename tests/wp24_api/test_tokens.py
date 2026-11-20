"""API tokens: scoped per tenant, hashed at rest, revocable, rate limited, audited, never human."""
from __future__ import annotations

from datetime import timedelta

from api.access import ROUTES, Access, app_routes, classify
from api.app import create_app
from api.context import CSRF_HEADER, TOKEN_RATE
from api.sessions import ApiSessionVerifier
from api.token_store import hash_token
from tests.wp24_api.conftest import auth
from thallo.contracts.actors import HumanActor

FAKE_ID = "00000000-0000-7000-8000-000000000000"


def _audit(w, tenant_id: str) -> list:  # noqa: ANN001
    with w.svc.backend.tenant(tenant_id) as tx:
        return list(tx.audit.store.entries(tenant_id))


def test_read_token_reads_own_tenant_only_and_every_call_is_audited(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id)
    c = w.client()
    before = len(_audit(w, w.a.tenant.id))
    r = c.get("/api/review/bundles", headers=auth(tok))
    assert r.status_code == 200, r.text
    ids = {b["bundle_id"] for b in r.json()["bundles"]}
    assert w.a.bundle_id in ids and w.b.bundle_id not in ids
    assert c.get(f"/api/review/bundles/{w.b.bundle_id}", headers=auth(tok)).status_code == 404
    entries = _audit(w, w.a.tenant.id)[before:]
    assert [e.action for e in entries] == ["api_token.call", "api_token.call"]
    assert all(e.actor_kind.value == "agent" for e in entries)
    assert entries[0].payload["route"] == "/api/review/bundles"


def test_token_is_hashed_at_rest_and_shown_once(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id)
    stored = w.svc.tokens.by_hash(hash_token(tok))
    assert stored is not None and stored.token_sha256 != tok and tok not in repr(stored)
    c, _ = w.login(w.a.founder_email, w.a.tenant.id)
    listed = c.get("/api/tokens").json()["tokens"]
    assert len(listed) == 1 and "token" not in listed[0] and tok not in str(listed)


def test_tokens_never_approve_send_or_reset(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id, scopes=("read", "draft"))
    c = w.client()
    for path in (f"/api/review/bundles/{w.a.bundle_id}/approve", f"/api/review/bundles/{w.a.bundle_id}/reject",
                 f"/api/leads/messages/{FAKE_ID}/send", f"/api/leads/{FAKE_ID}/book",
                 f"/api/trust/breakers/{FAKE_ID}/reset", "/api/trust/offer_post/raise",
                 f"/api/connections/{FAKE_ID}/secret"):
        r = c.post(path, json={}, headers=auth(tok))
        assert r.status_code == 403, (path, r.text)
        assert r.json()["error"] == "token_refused"
    refused = [e for e in _audit(w, w.a.tenant.id) if e.action == "api_token.refused"]
    assert len(refused) == 7


def test_verifier_refuses_a_token_session_on_its_own(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id)
    stored = w.svc.tokens.by_hash(hash_token(tok))
    actor = HumanActor(id=w.a.founder.id, tenant_id=w.a.tenant.id, session_id=f"api-token-{stored.id}")
    assert ApiSessionVerifier(w.svc.sessions).is_live_human_session(actor) is False


def test_every_route_is_classified_and_non_token_routes_refuse_bearer(w) -> None:  # noqa: ANN001
    app = create_app(w.svc)
    tok = w.token(w.a.founder_email, w.a.tenant.id, scopes=("read", "draft"))
    c = w.client()
    missing = []
    calls = 0
    routes = app_routes(app)
    assert len(routes) > 50
    for methods, template in routes:
        for method in methods - {"HEAD"}:
            if (method, template) not in ROUTES:
                missing.append((method, template))
            if (classify(method, template) in (Access.READ, Access.DRAFT) or template.startswith("/r/")
                    or template == "/api/mcp"):
                continue
            path = template
            for p in ("bundle_id", "variant_id", "slot_id", "connection_id", "code", "lead_id", "message_id",
                      "question_id", "answer_id", "shot_id", "task_type", "trip_id", "sentence_id", "token_id",
                      "template_id", "item_id",
                      "media_id"):
                path = path.replace("{" + p + "}", FAKE_ID)
            calls += 1
            if calls % 50 == 0:  # stay under the per-token rate limit as routes grow
                tok = w.token(w.a.founder_email, w.a.tenant.id, scopes=("read", "draft"))
            r = c.request(method, path, json={}, headers=auth(tok))
            assert r.status_code in (401, 403), (method, template, r.status_code, r.text)
    assert missing == [], f"classify these routes in api/access.py: {missing}"


def test_read_scope_cannot_draft_and_draft_scope_can(w) -> None:  # noqa: ANN001
    read = w.token(w.a.founder_email, w.a.tenant.id, scopes=("read",))
    draft = w.token(w.a.founder_email, w.a.tenant.id, scopes=("draft",))
    c = w.client()
    body = {"margin_per_won": "100", "share_of_margin": "0.1"}
    assert c.post("/api/growth/preview", json=body, headers=auth(read)).json()["error"] == "token_refused"
    r = c.post("/api/growth/preview", json=body, headers=auth(draft))
    assert r.status_code in (200, 422), r.text  # past the token check: the body is the route's business
    assert c.get("/api/plan", headers=auth(draft)).status_code == 403


def test_revoked_expired_and_malformed_tokens_fail(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id, days=1)
    c = w.client()
    assert c.get("/api/plan", headers=auth(tok)).status_code == 200
    w.clock.advance(days=1, seconds=1)
    assert c.get("/api/plan", headers=auth(tok)).status_code == 401
    tok2 = w.token(w.a.founder_email, w.a.tenant.id)
    stored = w.svc.tokens.by_hash(hash_token(tok2))
    admin, csrf = w.login(w.a.founder_email, w.a.tenant.id)
    assert admin.post(f"/api/tokens/{stored.id}/revoke", headers={CSRF_HEADER: csrf}).status_code == 200
    assert c.get("/api/plan", headers=auth(tok2)).status_code == 401
    assert c.get("/api/plan", headers={"authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/plan", headers={"authorization": "Basic abc"}).status_code == 401


def test_other_tenant_cannot_revoke(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id)
    stored = w.svc.tokens.by_hash(hash_token(tok))
    c, csrf = w.login(w.b.founder_email, w.b.tenant.id)
    assert c.post(f"/api/tokens/{stored.id}/revoke", headers={CSRF_HEADER: csrf}).status_code == 404
    assert w.client().get("/api/plan", headers=auth(tok)).status_code == 200


def test_rate_limit_per_token(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id)
    c = w.client()
    for _ in range(TOKEN_RATE):
        assert c.get("/api/plan", headers=auth(tok)).status_code == 200
    assert c.get("/api/plan", headers=auth(tok)).status_code == 429
    w.clock.advance(seconds=61)
    assert c.get("/api/plan", headers=auth(tok)).status_code == 200


def test_tokens_cannot_manage_tokens_and_staff_cannot_mint(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id)
    c = w.client()
    assert c.get("/api/tokens", headers=auth(tok)).status_code == 403
    assert c.post("/api/tokens", json={"name": "x", "scopes": ["read"]}, headers=auth(tok)).status_code == 403
    staff, csrf = w.login(w.a.staff_email, w.a.tenant.id)
    r = staff.post("/api/tokens", json={"name": "x", "scopes": ["read"]}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 403
    founder, csrf = w.login(w.a.founder_email, w.a.tenant.id)
    r = founder.post("/api/tokens", json={"name": "x", "scopes": ["approve"]}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 422
    r = founder.post("/api/tokens", json={"name": "x", "scopes": ["read"], "days": 365}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 422


def test_token_follows_its_maker(w) -> None:  # noqa: ANN001
    """The token acts with its maker's current role; a token lost with its tenant is useless elsewhere."""
    tok = w.token(w.both_email, w.a.tenant.id)
    c = w.client()
    ids = {b["bundle_id"] for b in c.get("/api/review/bundles", headers=auth(tok)).json()["bundles"]}
    assert ids == {w.a.bundle_id}  # never Beta, though the maker is a member of Beta too


def test_app_role_cannot_read_token_or_operator_tables(w) -> None:  # noqa: ANN001
    if w.kind != "postgres":
        return
    import psycopg

    with psycopg.connect(w.urls["app"]) as conn:
        for table in ("api_token", "operator_member"):
            try:
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                raise AssertionError(f"app role read {table}")
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()


def test_expiry_is_on_the_clock(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id, days=2)
    stored = w.svc.tokens.by_hash(hash_token(tok))
    assert stored.expires_at - w.clock.now() == timedelta(days=2)
