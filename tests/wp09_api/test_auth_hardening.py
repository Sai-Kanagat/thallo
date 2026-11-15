"""Security review findings on login and sessions: per-client and per-network limits, lockout per
identity across sessions, 8-digit codes, no shared global bucket, and compare-and-set sessions.

Client addresses: the test client's peer is the string "testclient" unless a test passes
`client=(ip, port)`. A request from 127.0.0.1 plays the Next server (web/server.mjs), whose
x-thallo-client-ip header is trusted; X-Forwarded-For is never read."""
from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from api.app import create_app
from api.context import COOKIE, CSRF_HEADER, DEVICE_COOKIE
from api.sessions import (
    CEILING_MAX,
    ISSUES_PER_EMAIL,
    LOCKOUT_AFTER,
    LOCKOUT_MAX,
    LOCKOUT_MAX_EXP,
    NETWORKS_PER_EMAIL,
    EmailCeiling,
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


def test_spoofed_headers_from_a_client_do_not_change_the_key(world):
    spoof = {"x-forwarded-for": "10.0.0.1", "x-thallo-client-ip": "10.0.0.1"}
    statuses = []
    for i in range(11):
        c, csrf = _client(world, "198.51.100.8")  # not the Next host: both headers are ignored
        statuses.append(_req(c, csrf, f"x{i}@f.test", **{**spoof, "x-thallo-client-ip": f"10.0.{i}.1"}).status_code)
    assert statuses[-1] == 429


def test_behind_the_next_server_two_real_peers_get_separate_buckets(world):
    """The reviewer's repro: every request arrives from 127.0.0.1. With the stamp, each browser is
    its own client; without it they would all share one bucket."""
    statuses = []
    for i in range(11):
        c, csrf = _client(world, "127.0.0.1")
        statuses.append(_req(c, csrf, f"p{i}@f.test", **{"x-thallo-client-ip": f"203.0.{i}.9"}).status_code)
    assert 429 not in statuses
    same = []
    for i in range(11):
        c, csrf = _client(world, "127.0.0.1")
        same.append(_req(c, csrf, f"q{i}@f.test", **{"x-thallo-client-ip": "198.51.100.33"}).status_code)
    assert same[-1] == 429
    # X-Forwarded-For through the Next host is still never read
    xff = []
    for i in range(11):
        c, csrf = _client(world, "127.0.0.1")
        xff.append(_req(c, csrf, f"r{i}@f.test", **{"x-thallo-client-ip": "198.51.100.44",
                                                     "x-forwarded-for": f"10.{i}.0.1"}).status_code)
    assert xff[-1] == 429


def test_the_stamp_is_trusted_only_from_configured_peers():
    from api.sessions import client_keys

    assert client_keys("127.0.0.1", "203.0.113.1")[0] != client_keys("127.0.0.1", "203.0.113.2")[0]
    assert client_keys("198.51.100.1", "203.0.113.1")[0] == client_keys("198.51.100.1", "203.0.113.2")[0]
    assert client_keys("127.0.0.1", "not-an-ip")[0] == client_keys("127.0.0.1", None)[0]
    peers = frozenset({"10.9.9.9"})
    assert client_keys("10.9.9.9", "203.0.113.1", proxy_peers=peers)[0] != client_keys(
        "10.9.9.9", "203.0.113.2", proxy_peers=peers)[0]


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


def _fill_ceiling(world, email: str) -> None:
    """Many networks each get a real code and guess wrong once: issued codes and bad networks."""
    for i in range(ISSUES_PER_EMAIL):
        c, csrf = _client(world, f"10.{i}.0.1")
        _req(c, csrf, email)
        c.post("/api/auth/verify", json={"email": email, "code": "00000009"}, headers={CSRF_HEADER: csrf})


def test_networks_without_wrong_guesses_do_not_count(world):
    email = world.a.founder_email
    for i in range(NETWORKS_PER_EMAIL + 2):  # plain requests, codes issued, no wrong guess
        c, csrf = _client(world, f"10.{i}.0.1")
        _req(c, csrf, email)
    calls = world.sender.calls
    c, csrf = _client(world, "10.200.0.1")
    _req(c, csrf, email)
    assert world.sender.calls == calls + 1


def test_unknown_device_owner_waits_at_most_the_capped_delay(world):
    email = world.a.founder_email
    _fill_ceiling(world, email)
    owner, o_csrf = _client(world, "198.51.100.77")
    calls = world.sender.calls
    assert _req(owner, o_csrf, email).status_code == 202 and world.sender.calls == calls  # same answer
    world.clock.advance(seconds=CEILING_MAX.total_seconds() + 1)
    owner, o_csrf = _client(world, "198.51.100.77")
    _req(owner, o_csrf, email)
    assert world.sender.calls == calls + 1  # a delay, never a lasting refusal
    r = owner.post("/api/auth/verify", json={"email": email, "code": world.sender.codes[email]},
                   headers={CSRF_HEADER: o_csrf})
    assert r.status_code == 200


def test_attackers_on_many_networks_cannot_stop_the_owner_on_a_known_device(world):
    email = world.a.founder_email
    owner, csrf = _client(world, "198.51.100.88")
    _req(owner, csrf, email)
    r = owner.post("/api/auth/verify", json={"email": email, "code": world.sender.codes[email]},
                   headers={CSRF_HEADER: csrf})
    assert r.status_code == 200 and DEVICE_COOKIE in owner.cookies
    owner.cookies.delete(COOKIE)  # the session is gone (closed tab, timeout); the device cookie stays
    _fill_ceiling(world, email)
    for i in range(2):  # ten wrong guesses lock the owner's own network for unknown devices
        a, a_csrf = _client(world, f"198.51.100.{i + 1}")
        _req(a, a_csrf, email)
        for _ in range(5):
            a.post("/api/auth/verify", json={"email": email, "code": "00000008"}, headers={CSRF_HEADER: a_csrf})
    stranger, s_csrf = _client(world, "198.51.100.99")
    calls = world.sender.calls
    _req(stranger, s_csrf, email)
    assert world.sender.calls == calls  # unknown devices wait
    csrf = owner.get("/api/session").json()["csrf_token"]
    assert _req(owner, csrf, email).status_code == 202
    assert world.sender.calls == calls + 1  # the known device gets its code at once
    r = owner.post("/api/auth/verify", json={"email": email, "code": world.sender.codes[email]},
                   headers={CSRF_HEADER: csrf})
    assert r.status_code == 200, r.text


def test_device_cookie_is_signed_and_bound_to_the_identity(world):
    d = world.svc.devices
    now = world.clock.now()
    token = d.make(world.a.founder.identity_id, now)
    assert d.valid_for(token, world.a.founder.identity_id, now)
    assert not d.valid_for(token, world.b.founder.identity_id, now)
    assert not d.valid_for(token[:-1] + ("0" if token[-1] != "0" else "1"), world.a.founder.identity_id, now)
    forged = token.replace(world.a.founder.identity_id, world.b.founder.identity_id)
    assert not d.valid_for(forged, world.b.founder.identity_id, now)


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

    a = client_keys("2001:db8:1:1::1", None)[1]
    b = client_keys("2001:db8:1:ffff::9", None)[1]
    c = client_keys("2001:db8:2::1", None)[1]
    assert a == b != c


# ---------------------------------------------------------------------- ceiling atomicity

def test_parallel_code_requests_issue_at_most_the_allowed_number(world):
    from concurrent.futures import ThreadPoolExecutor

    email = world.a.founder_email
    before = 15
    for i in range(before):
        c, csrf = _client(world, f"10.{i}.0.1")
        _req(c, csrf, email)
    start = world.sender.calls
    clients = [_client(world, f"10.{100 + i}.0.1") for i in range(20)]
    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(pool.map(lambda cc: _req(cc[0], cc[1], email).status_code, clients))
    assert set(statuses) == {202}
    assert world.sender.calls - start <= ISSUES_PER_EMAIL - before


def test_reserve_is_atomic_across_threads():
    import threading

    from thallo.contracts.clock import FAKE_EPOCH, FakeClock

    ceiling = EmailCeiling(FakeClock(FAKE_EPOCH))
    tokens: list = []
    barrier = threading.Barrier(40)

    def go(i):
        barrier.wait()
        tokens.append(ceiling.reserve("k", f"net{i}"))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for t in tokens if t is not None) == ISSUES_PER_EMAIL


def test_delay_still_holds_after_the_window_empties():
    from datetime import timedelta

    from thallo.contracts.clock import FAKE_EPOCH, FakeClock

    clock = FakeClock(FAKE_EPOCH)
    ceiling = EmailCeiling(clock)
    for i in range(ISSUES_PER_EMAIL):
        ceiling.issued(ceiling.reserve("k", f"n{i}"))
    for _ in range(4):  # keep hitting so the delay grows to its cap
        clock.advance(seconds=CEILING_MAX.total_seconds())
        for i in range(ISSUES_PER_EMAIL):
            t = ceiling.reserve("k", f"m{i}")
            if t:
                ceiling.issued(t)
    clock.advance(seconds=1)
    ceiling._issues["k"].clear()  # the sliding window is empty
    assert ceiling.reserve("k", "x") is None  # the running delay still holds
    clock.advance(seconds=CEILING_MAX.total_seconds())
    assert ceiling.reserve("k", "x") is not None  # and ends within the cap
    assert ceiling._until["k"] - clock.now() <= CEILING_MAX if "k" in ceiling._until else True
    assert CEILING_MAX == timedelta(minutes=15)


def test_failed_send_releases_the_slot(world):
    ceiling = world.svc.ceiling
    token = ceiling.reserve("k", "n")
    ceiling.release(token)
    assert ceiling._held == {} and not ceiling._issues.get("k")


# ---------------------------------------------------------------------- review round 3

def test_unknown_email_does_the_same_work_as_a_known_one(world, monkeypatch):
    calls = {"decoy": 0, "real": 0}
    monkeypatch.setattr(world.svc.decoy_sender, "send", lambda e, c: calls.__setitem__("decoy", calls["decoy"] + 1))
    issued = []
    real_issue, real_decoy = world.svc.codes.issue, world.svc.codes.decoy
    monkeypatch.setattr(world.svc.codes, "issue", lambda *a: issued.append("issue") or real_issue(*a))
    monkeypatch.setattr(world.svc.codes, "decoy", lambda: issued.append("decoy") or real_decoy())
    c, csrf = _client(world, "198.51.100.61")
    assert _req(c, csrf, "nobody@nowhere.test").status_code == 202
    assert calls["decoy"] == 1 and issued == ["decoy"] and world.sender.calls == 0
    c, csrf = _client(world, "198.51.100.62")
    assert _req(c, csrf, world.a.founder_email).status_code == 202
    assert issued == ["decoy", "issue"] and world.sender.calls == 1


def test_a_decoy_code_never_signs_anyone_in(world):
    c, csrf = _client(world, "198.51.100.63")
    _req(c, csrf, "nobody@nowhere.test")
    code = world.svc.codes.decoy()
    r = c.post("/api/auth/verify", json={"email": "nobody@nowhere.test", "code": code}, headers={CSRF_HEADER: csrf})
    assert r.status_code == 400


def test_sign_out_clears_the_device_cookie(world):
    c, csrf = world.login(world.a.founder_email)
    assert DEVICE_COOKIE in c.cookies
    r = c.post("/api/auth/logout", headers={CSRF_HEADER: csrf})
    assert r.status_code == 200
    cleared = [h for h in r.headers.get_list("set-cookie") if h.startswith(f"{DEVICE_COOKIE}=")]
    assert cleared and ("max-age=0" in cleared[0].lower() or "expires=thu, 01 jan 1970" in cleared[0].lower())
    assert DEVICE_COOKIE not in c.cookies
