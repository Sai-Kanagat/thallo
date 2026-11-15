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
from api.sessions import (
    LOCKOUT_AFTER,
    LOCKOUT_MAX,
    LOCKOUT_MAX_EXP,
    NETWORKS_PER_EMAIL,
    FailureLockout,
    InMemorySessionStore,
    SessionKind,
)


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


def _burn(world, ip: str, email: str, tries: int = 5) -> None:
    """Request a real code from `ip`, then guess wrong `tries` times."""
    c, csrf = _client(world, ip)
    _req(c, csrf, email)
    real = world.sender.codes[email]
    wrong = "00000001" if real != "00000001" else "00000002"
    for _ in range(tries):
        c.post("/api/auth/verify", json={"email": email, "code": wrong}, headers={CSRF_HEADER: csrf})


def test_guesses_without_an_issued_code_do_not_count(world):
    email = world.a.founder_email
    for i in range(LOCKOUT_AFTER + 5):
        c, csrf = _client(world, f"203.0.113.{i + 1}")
        c.post("/api/auth/verify", json={"email": email, "code": "00000001"}, headers={CSRF_HEADER: csrf})
    calls = world.sender.calls
    c, csrf = _client(world, "203.0.113.99")
    _req(c, csrf, email)
    assert world.sender.calls == calls + 1  # nothing was locked


def test_failures_lock_new_codes_for_that_network_exponentially(world):
    email = world.a.founder_email
    _burn(world, "203.0.113.1", email)
    _burn(world, "203.0.113.2", email)  # ten failures against real codes, from one /24
    calls = world.sender.calls
    c, csrf = _client(world, "203.0.113.3")
    assert _req(c, csrf, email).status_code == 202 and world.sender.calls == calls  # same answer, no code
    world.clock.advance(seconds=61)
    c, csrf = _client(world, "203.0.113.4")
    _req(c, csrf, email)
    assert world.sender.calls == calls + 1  # the first lock was one minute


def test_attacker_on_one_network_cannot_lock_out_the_owner_on_another(world):
    email = world.a.founder_email
    _burn(world, "203.0.113.1", email)
    _burn(world, "203.0.113.2", email)
    c, csrf = _client(world, "203.0.113.3")
    calls = world.sender.calls
    _req(c, csrf, email)
    assert world.sender.calls == calls  # the attacker's network is locked
    owner, o_csrf = _client(world, "198.51.100.10")
    assert _req(owner, o_csrf, email).status_code == 202
    r = owner.post("/api/auth/verify", json={"email": email, "code": world.sender.codes[email]},
                   headers={CSRF_HEADER: o_csrf})
    assert r.status_code == 200, r.text


def test_lockout_arithmetic_is_bounded_for_huge_counts(world):
    lock = FailureLockout(world.clock)
    for _ in range(10_000):
        lock.fail("k")
    assert lock._fails["k"] <= LOCKOUT_AFTER + LOCKOUT_MAX_EXP
    assert lock._until["k"] - world.clock.now() == LOCKOUT_MAX
    lock._fails["k"] = 10 ** 30  # even a corrupted count cannot overflow the wait
    lock.fail("k")
    assert lock._until["k"] - world.clock.now() <= LOCKOUT_MAX


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


# ---------------------------------------------------------------------- distributed guessing

def test_guesses_from_fifty_networks_cannot_pass_the_per_code_cap(world):
    email = world.a.founder_email
    first, csrf = _client(world, "198.51.100.1")
    _req(first, csrf, email)
    code = world.sender.codes[email]
    wrong = "00000003" if code != "00000003" else "00000004"
    cookie = dict(first.cookies)
    statuses = []
    for i in range(50):  # the same session, a new network each time (all the per-session tries)
        c = TestClient(create_app(world.svc), base_url="https://testserver", client=(f"10.{i}.0.1", 4000),
                       cookies=cookie)
        statuses.append(c.post("/api/auth/verify", json={"email": email, "code": wrong},
                               headers={CSRF_HEADER: csrf}).status_code)
    assert 200 not in statuses
    last = TestClient(create_app(world.svc), base_url="https://testserver", client=("10.99.0.1", 4000),
                      cookies=cookie)
    world.clock.advance(minutes=16)  # past every rate window; the code itself is gone
    r = last.post("/api/auth/verify", json={"email": email, "code": code}, headers={CSRF_HEADER: csrf})
    assert r.status_code != 200  # dead after five wrong guesses, whatever network they came from


def test_distinct_network_cap_stops_new_codes_only(world):
    email = world.a.founder_email
    for i in range(NETWORKS_PER_EMAIL):
        c, csrf = _client(world, f"10.{i}.0.1")
        _req(c, csrf, email)
    calls = world.sender.calls
    c, csrf = _client(world, "10.200.0.1")
    assert _req(c, csrf, email).status_code == 202 and world.sender.calls == calls  # same answer, no code


def test_owner_with_a_live_code_signs_in_while_the_global_ceiling_is_hit(world):
    email = world.a.founder_email
    owner, o_csrf = _client(world, "198.51.100.10")
    _req(owner, o_csrf, email)
    code = world.sender.codes[email]
    for i in range(NETWORKS_PER_EMAIL + 3):  # an attacker fills the ceiling from many networks
        a, a_csrf = _client(world, f"10.{i}.0.1")
        _req(a, a_csrf, email)
        for _ in range(5):
            a.post("/api/auth/verify", json={"email": email, "code": "00000005"}, headers={CSRF_HEADER: a_csrf})
    calls = world.sender.calls
    probe, p_csrf = _client(world, "10.250.0.1")
    _req(probe, p_csrf, email)
    assert world.sender.calls == calls  # the ceiling is hit: no new codes
    r = owner.post("/api/auth/verify", json={"email": email, "code": code}, headers={CSRF_HEADER: o_csrf})
    assert r.status_code == 200, r.text


def test_ipv6_networks_are_slash_48():
    from api.sessions import client_keys

    a = client_keys("2001:db8:1:1::1", None, trust_proxy=False)[1]
    b = client_keys("2001:db8:1:ffff::9", None, trust_proxy=False)[1]
    c = client_keys("2001:db8:2::1", None, trust_proxy=False)[1]
    assert a == b != c
