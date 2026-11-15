"""Postiz hosted and self-hosted: the provider contract, every failure mapped to "not sent" or
"unknown", bytes hashed as uploaded, and the key never going anywhere the operator did not name.
Replies come from postiz_fixtures.json (hand-written from the docs); sockets raise."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from thallo.connections import InMemorySecretStore, KeyRing, ProviderContext, ProviderUnavailable, Vault
from thallo.connections.errors import InvalidConnection
from thallo.connections.registry import ConnectionRegistry, ProviderRegistry
from thallo.connections.stores import InMemoryConnectionStore
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, Channel, ConnectionMode
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import T, _id
from thallo.providers.publishing import manual, postiz_hosted, postiz_self_hosted
from thallo.providers.publishing.postiz_base import PostizTransport, RawCall, RawReply, TransportError
from thallo.publish import LookupFailed, NotSent, OutcomeUnknown, SendContext, StagedMedia, staged

FIX = json.loads((Path(__file__).parent / "postiz_fixtures.json").read_text())
FAKE_KEY = "fake-postiz-key-for-tests-only"  # not a real key; never sent anywhere
SELF = "https://postiz.example.com"
SETTINGS = {"integrations": {"instagram": {"id": "ig-1", "provider": "instagram"}}}
IMAGE = b"\x89PNG fixture image"
SHA = hashlib.sha256(IMAGE).hexdigest()
TEXT = "Hello from the shop"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


class Fixtures:
    """Answers by (method, path) from the fixture file and records every call."""

    def __init__(self, **routes: str) -> None:
        self.routes = routes
        self.calls: list[RawCall] = []

    def __call__(self, call: RawCall) -> RawReply:
        self.calls.append(call)
        path = call.url.split("?")[0].rsplit("/", 1)[-1]
        name = self.routes.get(f"{call.method} {path}")
        if name is None:
            raise AssertionError(f"no fixture for {call.method} {path}")
        if name == "raise_reached":
            raise TransportError("the exchange failed (TimeoutError)", reached=True)
        if name == "raise_unreached":
            raise TransportError("could not connect (ConnectionRefusedError)", reached=False)
        f = FIX[name]
        return RawReply(f["status"], f["body"])


OK = {"POST upload": "upload_ok", "POST posts": "create_ok", "GET posts": "list_published",
      "GET integrations": "integrations"}


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock):
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


def ctx(clock, vault, name, *, settings=None, secret=FAKE_KEY):
    ref = vault.put(T, secret) if secret else None
    row = Connection(id=_id(70), created_at=clock.now(), tenant_id=T, capability=Capability.PUBLISHING,
                     provider=name, mode=ConnectionMode.HOSTED, settings=SETTINGS if settings is None else settings,
                     secret_ref=ref)
    return ProviderContext(T, row, clock, vault)


def hosted(clock, vault, transport, **kw):
    return postiz_hosted.create(ctx(clock, vault, postiz_hosted.NAME, **kw), transport=transport)


def self_hosted(clock, vault, transport, origin=SELF, **kw):
    return postiz_self_hosted.create(ctx(clock, vault, postiz_self_hosted.NAME, **kw), transport=transport,
                                     environ={postiz_self_hosted.ORIGIN_ENV: origin})


def request(media=(SHA,), key="variant-1:approval-1", **kw):
    base = dict(tenant_id=T, idempotency_key=key, channel=Channel.INSTAGRAM, channel_account_id=_id(11),
                variant_id=_id(12), approval_id=_id(13), content_hash="a" * 64, text=TEXT, media_sha256=media)
    return c.PublishRequest(**(base | kw))


def context(clock, media=(IMAGE,), key="variant-1:approval-1", text=TEXT):
    staged_media = tuple(StagedMedia(position=i, sha256=hashlib.sha256(m).hexdigest(), mime="image/png",
                                     alt_text=None, data=m) for i, m in enumerate(media))
    return SendContext(tenant_id=T, idempotency_key=key, channel="instagram", text=text,
                       text_sha256=hashlib.sha256(text.encode()).hexdigest(), requested_at=clock.now(),
                       media=staged_media)


READBACK = c.ReadbackRequest(tenant_id=T, idempotency_key="variant-1:approval-1", publication_id="p1",
                             external_id="post-123")


# ---------------------------------------------------------------- the contract, all three providers

@pytest.mark.parametrize("make", [hosted, self_hosted])
def test_postiz_passes_the_provider_contract(clock, vault, make):
    prov = make(clock, vault, Fixtures(**OK))
    assert prov.health().connected
    with staged(context(clock)):
        assert_provider_contract(prov, clock, requests=[request(), READBACK])
    info = prov.describe()
    assert info.sends_externally and info.mode is ConnectionMode.HOSTED


def test_manual_pack_passes_the_provider_contract(clock):
    row = Connection(id=_id(71), created_at=clock.now(), tenant_id=T, capability=Capability.PUBLISHING,
                     provider="manual", mode=ConnectionMode.MANUAL)
    assert_provider_contract(manual.create(ProviderContext(T, row, clock)), clock)


def test_a_publish_sends_the_bytes_and_text_it_was_given_with_the_bare_key(clock, vault):
    fx = Fixtures(**OK)
    prov = hosted(clock, vault, fx)
    with staged(context(clock)):
        result = prov.run(request())
    assert result.output.status == "published" and result.output.external_id == "post-123"
    upload, create = fx.calls
    assert upload.url == "https://api.postiz.com/public/v1/upload" and IMAGE in upload.body
    assert upload.content_type.startswith("multipart/form-data")
    body = json.loads(create.body)
    assert body["type"] == "now" and body["posts"][0]["value"][0]["content"] == TEXT
    assert body["posts"][0]["value"][0]["image"] == [{"id": "img-123", "path": "https://uploads.postiz.invalid/img-123.png"}]
    assert body["posts"][0]["integration"] == {"id": "ig-1"}
    assert create.auth.header == "authorization" and create.auth.scheme == ""  # no "Bearer"
    assert create.auth.secret.reveal() == FAKE_KEY


def test_self_hosted_calls_only_the_operator_origin(clock, vault):
    fx = Fixtures(**OK)
    with staged(context(clock)):
        self_hosted(clock, vault, fx).run(request())
    assert {call.url.split("?")[0].rsplit("/", 1)[0] for call in fx.calls} == {SELF + "/api/public/v1"}


# ---------------------------------------------------------------- only the publisher's bytes

def test_no_staged_send_sends_nothing(clock, vault):
    fx = Fixtures(**OK)
    with pytest.raises(NotSent):
        hosted(clock, vault, fx).run(request())
    assert fx.calls == []


def test_staged_bytes_that_do_not_hash_to_the_request_send_nothing(clock, vault):
    fx = Fixtures(**OK)
    bad = context(clock)
    tampered = SendContext(**{**bad.__dict__, "media": (StagedMedia(0, SHA, "image/png", None, IMAGE + b"x"),)})
    with staged(tampered), pytest.raises(NotSent, match="do not hash"):
        hosted(clock, vault, fx).run(request())
    assert fx.calls == []


def test_a_request_whose_text_is_not_the_staged_text_sends_nothing(clock, vault):
    fx = Fixtures(**OK)
    with staged(context(clock)), pytest.raises(NotSent, match="approved text"):
        hosted(clock, vault, fx).run(request(text="Hello from the shop!"))
    assert fx.calls == []


# ---------------------------------------------------------------- failures: not sent or unknown, never guessed

@pytest.mark.parametrize(("create", "error", "retryable"), [
    ("rate_limited", NotSent, True), ("bad_request", NotSent, False), ("raise_unreached", NotSent, True),
    ("server_error", OutcomeUnknown, None), ("raise_reached", OutcomeUnknown, None),
    ("create_no_id", OutcomeUnknown, None),
])
def test_create_post_failures_map_to_not_sent_or_unknown(clock, vault, create, error, retryable):
    prov = hosted(clock, vault, Fixtures(**{**OK, "POST posts": create}))
    with staged(context(clock)), pytest.raises(error) as got:
        prov.run(request())
    if retryable is not None:
        assert got.value.retryable is retryable


def test_a_lost_upload_is_not_a_post(clock, vault):
    fx = Fixtures(**{**OK, "POST upload": "raise_reached"})
    with staged(context(clock)), pytest.raises(NotSent):
        hosted(clock, vault, fx).run(request())
    assert [call.url.rsplit("/", 1)[-1] for call in fx.calls] == ["upload"]  # create never called


# ---------------------------------------------------------------- readback and reconcile

def test_readback_by_id_returns_postiz_text_and_the_live_link(clock, vault):
    with staged(context(clock)):
        result = hosted(clock, vault, Fixtures(**OK)).run(READBACK)
    assert result.output.live_text == TEXT and result.output.media_changed is None
    assert result.external_ids == {"external_id": "post-123", "state": "PUBLISHED",
                                   "live_url": "https://instagram.invalid/p/abc"}


def test_reconcile_finds_the_post_by_integration_and_canonical_text(clock, vault):
    lookup = READBACK.model_copy(update={"external_id": None})
    with staged(context(clock, text="Hello   from the shop")):
        result = hosted(clock, vault, Fixtures(**OK)).run(lookup)
    assert result.external_ids["external_id"] == "post-123"


def test_reconcile_with_nothing_found_says_so(clock, vault):
    lookup = READBACK.model_copy(update={"external_id": None})
    with staged(context(clock)):
        result = hosted(clock, vault, Fixtures(**{**OK, "GET posts": "list_empty"})).run(lookup)
    assert result.output.live_text is None and result.external_ids == {}


def test_two_matching_posts_are_never_guessed(clock, vault):
    lookup = READBACK.model_copy(update={"external_id": None})
    with staged(context(clock)), pytest.raises(LookupFailed, match="more than one"):
        hosted(clock, vault, Fixtures(**{**OK, "GET posts": "list_twice"})).run(lookup)


def test_a_queued_post_is_not_live_text(clock, vault):
    with staged(context(clock)):
        result = hosted(clock, vault, Fixtures(**OK)).run(READBACK.model_copy(update={"external_id": "post-999"}))
    assert result.output.live_text is None and result.external_ids["state"] == "QUEUE"


# ---------------------------------------------------------------- where the key may go

@pytest.mark.parametrize("origin", [
    "", "http://postiz.example.com", "https://Postiz.example.com", "https://postiz.example.com/api",
    "https://a.example.com,https://b.example.com", "https://user@postiz.example.com", "https://postiz.example.com.",
    "https://postiz.example.com?x=1", " https://postiz.example.com\n", "https://postiz.example.com:99999",
])
def test_a_hostile_or_malformed_self_hosted_origin_is_unavailable_and_never_called(clock, vault, origin):
    fx = Fixtures(**OK)
    prov = self_hosted(clock, vault, fx, origin=origin)
    if origin.strip() == SELF:  # surrounding whitespace is trimmed from an env value; the origin itself is fine
        return
    assert not prov.health().connected
    with staged(context(clock)):
        for req in (request(), READBACK):
            with pytest.raises(ProviderUnavailable):
                prov.estimate(req)
            with pytest.raises(ProviderUnavailable):
                prov.run(req)
    assert fx.calls == []


def test_a_tenant_setting_cannot_name_a_host(clock, vault):
    providers = ProviderRegistry.with_discovered()
    reg = ConnectionRegistry(clock=clock, providers=providers, connections=InMemoryConnectionStore(), vault=vault,
                             environ={})
    for bad in ({**SETTINGS, "base_url": "https://evil.example"}, {**SETTINGS, "origin": SELF},
                {"integrations": {"instagram": {"id": "ig-1", "provider": "instagram", "url": "https://evil"}}}):
        for name in (postiz_hosted.NAME, postiz_self_hosted.NAME):
            with pytest.raises(InvalidConnection):
                reg.add(T, Capability.PUBLISHING, name, settings=bad, secret=FAKE_KEY)


def test_unconfigured_postiz_falls_through_to_manual(clock, vault):
    for prov in (hosted(clock, vault, Fixtures(), settings={}),
                 hosted(clock, vault, Fixtures(), secret=None),
                 self_hosted(clock, vault, Fixtures(), origin="")):
        with pytest.raises(ProviderUnavailable):
            prov.estimate(request())
    other = request(channel=Channel.FACEBOOK)
    with pytest.raises(ProviderUnavailable, match="no Postiz integration"):
        hosted(clock, vault, Fixtures()).estimate(other)


def test_a_key_with_a_newline_is_never_sent(clock, vault):
    fx = Fixtures(**OK)
    prov = hosted(clock, vault, fx)  # the real vault refuses such a value, so a double hands it back
    prov.ctx = ProviderContext(T, prov.ctx.connection, clock, _Leaky("bad\nkey"))
    with staged(context(clock)), pytest.raises(NotSent, match="header") as got:
        prov.run(request())
    assert fx.calls == [] and "bad" not in str(got.value)


class _Leaky:
    """A vault double that hands back a value the real vault would refuse to store."""

    def __init__(self, value: str) -> None:
        self.value = value

    def resolve(self, tenant_id, ref):
        from thallo.connections.secret import Secret
        return Secret(self.value)


# ---------------------------------------------------------------- the real transport, with no network

class Conn:
    """A stand-in for http.client's pinned connection: records what it was asked to do."""

    log: list[str] = []

    def __init__(self, scheme, host, port, ip, timeout, fail_connect=False):
        self.fail_connect = fail_connect
        self.headers = None

    def connect(self):
        Conn.log.append("connect")
        if self.fail_connect:
            raise ConnectionRefusedError

    def request(self, method, path, body=None, headers=None):
        Conn.log.append(f"request {headers.get('authorization')}")
        raise TimeoutError

    def getresponse(self):
        raise AssertionError

    def close(self):
        Conn.log.append("close")


def call(key: str = FAKE_KEY) -> RawCall:
    from thallo.connections.secret import Secret
    from thallo.providers.text.base import Auth
    return RawCall("POST", "https://postiz.example.com/api/public/v1/posts", b"{}", "application/json",
                   Auth("authorization", Secret(key), ""))


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "::ffff:10.0.0.1", "64:ff9b::a00:1"])
def test_a_private_address_is_never_connected_to(ip):
    Conn.log = []
    t = PostizTransport(resolver=lambda host, port: [ip], connect=Conn)
    with pytest.raises(TransportError) as got:
        t(call())
    assert got.value.reached is False and Conn.log == []


def test_a_failed_connect_never_reveals_the_key():
    Conn.log = []
    t = PostizTransport(resolver=lambda h, p: ["93.184.216.34"],
                        connect=lambda *a: Conn(*a, fail_connect=True))
    with pytest.raises(TransportError) as got:
        t(call())
    assert got.value.reached is False and Conn.log == ["connect", "close"]


def test_a_failure_after_the_request_left_is_reached_and_the_error_carries_no_key():
    Conn.log = []
    t = PostizTransport(resolver=lambda h, p: ["93.184.216.34"], connect=Conn)
    with pytest.raises(TransportError) as got:
        t(call())
    assert got.value.reached is True and FAKE_KEY not in str(got.value)
    assert Conn.log == ["connect", f"request {FAKE_KEY}", "close"]


def test_http_is_refused_before_any_lookup():
    seen = []
    t = PostizTransport(resolver=lambda h, p: seen.append(h) or ["93.184.216.34"], connect=Conn)
    with pytest.raises(TransportError):
        t(RawCall("GET", "http://postiz.example.com/api/public/v1/posts"))
    assert seen == []
