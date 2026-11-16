"""Login by identity with a one-time code, tenant switch, sessions, CSRF, rate limits, headers."""
from __future__ import annotations

from datetime import timedelta

from api.context import COOKIE, CSRF_HEADER
from api.sessions import IDLE_TIMEOUT


def _start(world):
    c = world.client()
    r = c.get("/api/session")
    assert r.status_code == 200
    return c, r.json()["csrf_token"]


def test_login_with_code_then_reads_own_tenant(world):
    c, _ = world.login(world.a.founder_email)
    r = c.get("/api/session").json()
    assert r["authenticated"] is True
    assert r["tenant"]["tenant_id"] == world.a.tenant.id
    assert r["tenant"]["role"] == "founder"


def test_cookie_flags(world):
    c = world.client()
    r = c.get("/api/session")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in cookie.lower()


def test_unknown_and_known_email_get_the_same_answer(world):
    c, csrf = _start(world)
    known = c.post("/api/auth/request-code", json={"email": world.a.founder_email}, headers={CSRF_HEADER: csrf})
    c2, csrf2 = _start(world)
    unknown = c2.post("/api/auth/request-code", json={"email": "nobody@nowhere.test"}, headers={CSRF_HEADER: csrf2})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert world.sender.calls == 1  # only the known address got a code


def test_wrong_code_and_unknown_email_fail_the_same_way(world):
    c, csrf = _start(world)
    c.post("/api/auth/request-code", json={"email": world.a.founder_email}, headers={CSRF_HEADER: csrf})
    wrong = c.post("/api/auth/verify", json={"email": world.a.founder_email, "code": "00000000"},
                   headers={CSRF_HEADER: csrf})
    ghost = c.post("/api/auth/verify", json={"email": "nobody@nowhere.test", "code": "12345678"},
                   headers={CSRF_HEADER: csrf})
    assert wrong.status_code == ghost.status_code == 400
    assert wrong.json() == ghost.json()


def test_code_is_bound_to_the_session_that_asked(world):
    c, csrf = _start(world)
    c.post("/api/auth/request-code", json={"email": world.a.founder_email}, headers={CSRF_HEADER: csrf})
    other, other_csrf = _start(world)
    r = other.post("/api/auth/verify", json={"email": world.a.founder_email,
                                             "code": world.sender.codes[world.a.founder_email]},
                   headers={CSRF_HEADER: other_csrf})
    assert r.status_code == 400


def test_code_expires_and_burns_after_attempts(world):
    c, csrf = _start(world)
    email = world.a.founder_email
    c.post("/api/auth/request-code", json={"email": email}, headers={CSRF_HEADER: csrf})
    code = world.sender.codes[email]
    for _ in range(5):
        c.post("/api/auth/verify", json={"email": email, "code": "99999999" if code != "99999999" else "11111111"},
               headers={CSRF_HEADER: csrf})
    r = c.post("/api/auth/verify", json={"email": email, "code": code}, headers={CSRF_HEADER: csrf})
    assert r.status_code in (400, 429)  # burned after five tries (or rate limited)

    c2, csrf2 = _start(world)
    c2.post("/api/auth/request-code", json={"email": world.b.founder_email}, headers={CSRF_HEADER: csrf2})
    world.clock.advance(minutes=11)
    c2.get("/api/session")  # keep the anonymous session alive is not enough: the code is gone
    email_b = world.b.founder_email
    r = c2.post("/api/auth/verify", json={"email": email_b, "code": world.sender.codes[email_b]},
                headers={CSRF_HEADER: csrf2})
    assert r.status_code in (400, 401, 403)


def test_request_code_is_rate_limited_per_email(world):
    email = world.a.founder_email
    statuses = []
    for _ in range(6):
        c, csrf = _start(world)
        r = c.post("/api/auth/request-code", json={"email": email}, headers={CSRF_HEADER: csrf})
        statuses.append(r.status_code)
    assert statuses[:5] == [202] * 5
    assert statuses[5:] == [429]
    # an unknown address is limited the same way, so the limit reveals nothing
    ghost = []
    for _ in range(4):  # stays under the per-client cap of 10
        c, csrf = _start(world)
        ghost.append(c.post("/api/auth/request-code", json={"email": "x@ghost.test"},
                            headers={CSRF_HEADER: csrf}).status_code)
    assert ghost == [202, 202, 202, 202]  # same as a known address at the same count


def test_verify_is_rate_limited(world):
    c, csrf = _start(world)
    email = world.a.founder_email
    c.post("/api/auth/request-code", json={"email": email}, headers={CSRF_HEADER: csrf})
    codes = [c.post("/api/auth/verify", json={"email": email, "code": "12312312"},
                    headers={CSRF_HEADER: csrf}).status_code for _ in range(12)]
    assert 429 in codes


def test_csrf_missing_is_refused_before_login(world):
    c, _ = _start(world)
    r = c.post("/api/auth/request-code", json={"email": world.a.founder_email})
    assert r.status_code == 403 and r.json()["error"] == "csrf"
    r = c.post("/api/auth/request-code", json={"email": world.a.founder_email}, headers={CSRF_HEADER: "forged"})
    assert r.status_code == 403
    assert world.sender.calls == 0


def test_cross_site_request_refused(world):
    c, csrf = _start(world)
    r = c.post("/api/auth/request-code", json={"email": world.a.founder_email},
               headers={CSRF_HEADER: csrf, "sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_session_rotates_on_login_and_on_tenant_switch(world):
    c, csrf = _start(world)
    anon_cookie = c.cookies.get(COOKIE)
    email = world.both_email
    c.post("/api/auth/request-code", json={"email": email}, headers={CSRF_HEADER: csrf})
    r = c.post("/api/auth/verify", json={"email": email, "code": world.sender.codes[email]},
               headers={CSRF_HEADER: csrf})
    assert r.status_code == 200
    body = r.json()
    assert {m["tenant_id"] for m in body["memberships"]} == {world.a.tenant.id, world.b.tenant.id}
    assert body["tenant"] is None  # two memberships: the person picks
    logged_cookie = c.cookies.get(COOKIE)
    assert logged_cookie != anon_cookie
    assert body["csrf_token"] != csrf
    # the old anonymous token no longer works
    stale = world.client()
    stale.cookies.set(COOKIE, anon_cookie)
    assert stale.get("/api/session").json()["authenticated"] is False

    r = c.post("/api/auth/tenant", json={"tenant_id": world.b.tenant.id}, headers={CSRF_HEADER: body["csrf_token"]})
    assert r.status_code == 200
    assert r.json()["tenant"]["tenant_id"] == world.b.tenant.id
    assert c.cookies.get(COOKIE) != logged_cookie
    assert r.json()["csrf_token"] != body["csrf_token"]


def test_tenant_switch_to_a_tenant_without_membership_is_not_found(world):
    c, csrf = world.login(world.a.founder_email)
    r = c.post("/api/auth/tenant", json={"tenant_id": world.b.tenant.id}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 404
    assert c.get("/api/session").json()["tenant"]["tenant_id"] == world.a.tenant.id


def test_idle_timeout_signs_out(world):
    c, _ = world.login(world.a.founder_email)
    world.clock.advance(seconds=IDLE_TIMEOUT.total_seconds() + 1)
    assert c.get("/api/review/bundles").status_code == 401


def test_activity_keeps_the_session_alive(world):
    c, _ = world.login(world.a.founder_email)
    for _ in range(3):
        world.clock.advance(seconds=(IDLE_TIMEOUT - timedelta(minutes=1)).total_seconds())
        assert c.get("/api/review/bundles").status_code == 200


def test_logout_needs_csrf_and_ends_the_session(world):
    c, csrf = world.login(world.a.founder_email)
    assert c.post("/api/auth/logout").status_code == 403
    assert c.post("/api/auth/logout", headers={CSRF_HEADER: csrf}).status_code == 200
    assert c.get("/api/review/bundles").status_code == 401


def test_bearer_token_is_not_a_session(world):
    c = world.client()
    r = c.get("/api/review/bundles", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 401


def test_security_headers_on_every_response(world):
    c = world.client()
    for r in (c.get("/api/session"), c.get("/api/nothing-here")):
        assert r.headers["content-security-policy"].startswith("default-src 'none'")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"
    assert c.get("/api/nothing-here").json() == {"error": "not_found", "message": "not found"}


def test_validation_errors_do_not_echo_input(world):
    c, csrf = _start(world)
    r = c.post("/api/auth/request-code", json={"email": "x" * 400, "extra": "do-not-echo-me"},
               headers={CSRF_HEADER: csrf})
    assert r.status_code == 422
    assert "do-not-echo-me" not in r.text and "xxxx" not in r.text
