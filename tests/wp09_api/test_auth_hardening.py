"""Security review findings on login and sessions: per-client and per-network limits, lockout per
identity across sessions, 8-digit codes, no shared global bucket, and compare-and-set sessions.

Client addresses: the test client's peer is the string "testclient" unless a test passes
`client=(ip, port)`. X-Forwarded-For is ignored unless `Services.trust_proxy` is set (the operator
setting THALLO_TRUSTED_PROXY=1), and then only its last entry is used."""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from api.app import create_app
from api.context import CSRF_HEADER
from api.sessions import LOCKOUT_AFTER, InMemorySessionStore, SessionKind


def _client(world, ip: str | None = None, port: int = 50000) -> tuple[TestClient, str]:
    kw = {"client": (ip, port)} if ip else {}
    c = TestClient(create_app(world.svc), base_url="https://testserver", **kw)
    return c, c.get("/api/session").json()["csrf_token"]


def _req(c, csrf, email, **headers):
    return c.post("/api/auth/request-code", json={"email": email}, headers={CSRF_HEADER: csrf, **headers})


def test_codes_have_eight_digits(world):
    c, csrf = _client(world)
    _req(c, csrf, world.a.founder_email)
    code = world.sender.codes[world.a.founder_email]
    assert len(code) == 8 and code.isdigit()


def test_request_code_limited_per_client_across_emails(world):
    statuses = []
    for i in range(12):
        c, csrf = _client(world, "198.51.100.7")
        statuses.append(_req(c, csrf, f"user{i}@spray.test").status_code)
    assert statuses[:10] == [202] * 10 and statuses[10:] == [429, 429]
    # another client elsewhere is not affected: no shared global bucket
    c, csrf = _client(world, "203.0.113.50")
    assert _req(c, csrf, world.a.founder_email).status_code == 202


def test_network_backstop_covers_many_addresses_in_one_slash_24(world):
    statuses = []
    for i in range(32):
        c, csrf = _client(world, f"192.0.2.{i + 1}")
        statuses.append(_req(c, csrf, f"n{i}@spray.test").status_code)
    assert statuses.count(429) == 2  # 30 per /24 per window
    c, csrf = _client(world, "192.0.3.1")
    assert _req(c, csrf, "fresh@other.test").status_code == 202


def test_forwarded_for_is_ignored_unless_a_proxy_is_trusted(world):
    statuses = [(_req(*_client(world, "198.51.100.8"), f"x{i}@f.test", **{"x-forwarded-for": f"10.0.{i}.1"})
                 .status_code) for i in range(11)]
    assert statuses[-1] == 429  # the spoofed header did not make new clients
    world.svc.trust_proxy = True
    statuses = [(_req(*_client(world, "198.51.100.9"), f"y{i}@f.test",
                      **{"x-forwarded-for": f"6.6.6.6, 10.{i}.0.1"}).status_code) for i in range(11)]
    assert 429 not in statuses  # behind a trusted proxy the last entry is the client


def test_failures_across_sessions_lock_new_codes_exponentially(world):
    email = world.a.founder_email
    for i in range(LOCKOUT_AFTER):  # failures from many sessions and clients add up per identity
        c, csrf = _client(world, f"203.0.113.{i + 1}")
        c.post("/api/auth/verify", json={"email": email, "code": "00000001"}, headers={CSRF_HEADER: csrf})
    calls = world.sender.calls
    c, csrf = _client(world, "198.51.100.200")
    r = _req(c, csrf, email)
    assert r.status_code == 202 and world.sender.calls == calls  # same answer, no code sent
    world.clock.advance(seconds=61)
    c, csrf = _client(world, "198.51.100.201")
    assert _req(c, csrf, email).status_code == 202 and world.sender.calls == calls + 1
    # one more failure doubles the wait
    c2, csrf2 = _client(world, "198.51.100.202")
    c2.post("/api/auth/verify", json={"email": email, "code": "00000001"}, headers={CSRF_HEADER: csrf2})
    world.clock.advance(seconds=61)
    c3, csrf3 = _client(world, "198.51.100.203")
    _req(c3, csrf3, email)
    assert world.sender.calls == calls + 1  # still locked: 2 minutes now


def test_owner_with_a_valid_code_is_not_blocked_by_attacks_on_the_address(world):
    email = world.a.founder_email
    owner, csrf = _client(world, "198.51.100.10")
    _req(owner, csrf, email)
    code = world.sender.codes[email]
    for i in range(LOCKOUT_AFTER + 5):
        a, a_csrf = _client(world, f"203.0.113.{100 + i}")
        a.post("/api/auth/verify", json={"email": email, "code": "00000002"}, headers={CSRF_HEADER: a_csrf})
    r = owner.post("/api/auth/verify", json={"email": email, "code": code}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------- compare-and-set sessions

def test_touch_and_save_cannot_resurrect_a_deleted_session(world):
    store = InMemorySessionStore(world.clock)
    token, s = store.create()
    store.delete(s)
    assert store.touch(s) is None
    assert store.save(replace(s, authenticated=True)) is False
    assert store.by_token(token) is None and store.by_id(s.id) is None


def test_save_after_rotation_does_not_revive_the_old_token(world):
    store = InMemorySessionStore(world.clock)
    old_token, s = store.create()
    new_token, fresh = store.rotate(s, authenticated=True)
    assert store.save(replace(s, authenticated=True, tenant_id=world.a.tenant.id)) is False
    assert store.by_token(old_token) is None and store.by_id(s.id) is None
    assert store.by_token(new_token).id == fresh.id


def test_revoked_hashes_are_kept_until_lifetime_ends(world):
    store = InMemorySessionStore(world.clock)
    token, s = store.create(SessionKind.INTERACTIVE)
    store.delete(s)
    assert s.token_hash in store._revoked
    world.clock.advance(seconds=store.lifetime.total_seconds() + 1)
    assert store.by_token(token) is None
    assert s.token_hash not in store._revoked  # pruned once its lifetime is over


def test_logout_during_a_request_cannot_be_undone_by_a_late_touch(world):
    c, csrf = world.login(world.a.founder_email)
    live = next(x for x in world.svc.sessions._by_hash.values() if x.app_user_id == world.a.founder.id)
    world.svc.sessions.delete(live)  # logout lands first
    assert world.svc.sessions.touch(live) is None  # the late touch of the in-flight request
    assert c.get("/api/review/bundles").status_code == 401
