"""The Business Profile publishing provider: contract suite, failure mapping, origins, and the
shapes it refuses so the chain falls to the manual pack. Replies come from gbp_fixtures.json."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from thallo.connections import InMemorySecretStore, KeyRing, ProviderContext, ProviderUnavailable, Vault
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, Channel, ConnectionMode
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import T, _id
from thallo.providers.publishing import google_business_profile as gbp
from thallo.providers.publishing.postiz_base import RawCall, RawReply, TransportError
from thallo.publish import AmbiguousLookup, NotSent, OutcomeUnknown, SendContext, StagedMedia, staged

FIX = json.loads((Path(__file__).parent / "gbp_fixtures.json").read_text())
CRED = json.dumps({"client_id": "fake-id", "client_secret": "fake-secret", "refresh_token": "fake-refresh"})
SETTINGS = {"account_id": "111", "location_id": "222", "language_code": "it"}
TEXT = "Open late on Fridays"
KEY = "variant-1:approval-1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


class Fixtures:
    def __init__(self, **routes: str) -> None:
        self.routes = routes
        self.calls: list[RawCall] = []

    def __call__(self, call: RawCall) -> RawReply:
        self.calls.append(call)
        if call.url == gbp.TOKEN_URL:
            key = "token"
        else:
            path = call.url.split("?")[0]
            key = f"{call.method} " + ("list" if path.endswith("/localPosts") else "post")
        name = self.routes.get(key)
        if name is None:
            raise AssertionError(f"no fixture for {key}")
        if name == "raise_reached":
            raise TransportError("the exchange failed (TimeoutError)", reached=True)
        if name == "raise_unreached":
            raise TransportError("could not connect", reached=False)
        f = FIX[name]
        return RawReply(f["status"], f["body"])


OK = {"token": "token_ok", "POST list": "create_ok", "GET list": "list_one", "GET post": "get_live"}


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock):
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


def make(clock, vault, transport, *, settings=None, secret=CRED):
    ref = vault.put(T, secret) if secret else None
    row = Connection(id=_id(70), created_at=clock.now(), tenant_id=T, capability=Capability.PUBLISHING,
                     provider=gbp.NAME, mode=ConnectionMode.HOSTED,
                     settings=SETTINGS if settings is None else settings, secret_ref=ref)
    return gbp.create(ProviderContext(T, row, clock, vault), transport=transport)


def request(**kw):
    base = dict(tenant_id=T, idempotency_key=KEY, channel=Channel.GOOGLE_BUSINESS_PROFILE, channel_account_id=_id(11),
                variant_id=_id(12), approval_id=_id(13), content_hash="a" * 64, text=TEXT)
    return c.PublishRequest(**(base | kw))


def context(clock, text=TEXT, media=()):
    sm = tuple(StagedMedia(position=i, sha256=hashlib.sha256(m).hexdigest(), mime="image/png", alt_text=None, data=m)
               for i, m in enumerate(media))
    return SendContext(tenant_id=T, idempotency_key=KEY, channel="google_business_profile", text=text,
                       text_sha256=hashlib.sha256(text.encode()).hexdigest(), requested_at=clock.now(), media=sm)


READBACK = c.ReadbackRequest(tenant_id=T, idempotency_key=KEY, publication_id="p1",
                             external_id="accounts/111/locations/222/localPosts/p-1")


def test_passes_the_provider_contract(clock, vault):
    prov = make(clock, vault, Fixtures(**OK))
    assert prov.health().connected
    with staged(context(clock)):
        assert_provider_contract(prov, clock, requests=[request(), READBACK])
    info = prov.describe()
    assert info.sends_externally and info.limits["media"] is False


def test_registered_by_discovery():
    from thallo.connections.registry import ProviderRegistry
    assert gbp.NAME in ProviderRegistry.with_discovered().names(Capability.PUBLISHING)


def test_publish_sends_the_text_with_the_token_only_to_google(clock, vault):
    fx = Fixtures(**OK)
    prov = make(clock, vault, fx)
    with staged(context(clock)):
        out = prov.run(request())
    assert out.output.status == "published"
    assert out.output.external_id == "accounts/111/locations/222/localPosts/p-1"
    token, create = fx.calls
    assert token.url == gbp.TOKEN_URL and token.auth is None and b"fake-refresh" in token.body
    assert create.url == "https://mybusiness.googleapis.com/v4/accounts/111/locations/222/localPosts"
    assert create.auth.secret.reveal() == "fake-access-token" and create.auth.scheme == "Bearer "
    assert json.loads(create.body) == {"languageCode": "it", "summary": TEXT, "topicType": "STANDARD"}
    assert b"fake-refresh" not in create.body


@pytest.mark.parametrize("kw, why", [
    ({"media_sha256": ("b" * 64,)}, "photos"),
    ({"channel": Channel.INSTAGRAM}, "google_business_profile"),
    ({"text": "x" * 1501}, "1500"),
])
def test_shapes_it_does_not_post_fall_through(clock, vault, kw, why):
    prov = make(clock, vault, Fixtures(**OK))
    with pytest.raises(ProviderUnavailable, match=why):
        prov.estimate(request(**kw))


def test_unconfigured_is_unavailable(clock, vault):
    with pytest.raises(ProviderUnavailable):
        make(clock, vault, Fixtures(**OK), settings={"account_id": "x/../y", "location_id": "2"}).estimate(request())
    with pytest.raises(ProviderUnavailable):
        make(clock, vault, Fixtures(**OK), secret=None).estimate(request())


def test_no_staged_send_refuses(clock, vault):
    fx = Fixtures(**OK)
    with pytest.raises(NotSent):
        make(clock, vault, fx).run(request())
    assert fx.calls == []


@pytest.mark.parametrize("route, exc, retry", [
    ({"token": "token_refused"}, NotSent, False),
    ({"token": "raise_unreached"}, NotSent, True),
    ({"POST list": "rate_limited"}, NotSent, True),
    ({"POST list": "bad_request"}, NotSent, False),
    ({"POST list": "raise_unreached"}, NotSent, True),
    ({"POST list": "raise_reached"}, OutcomeUnknown, None),
    ({"POST list": "server_error"}, OutcomeUnknown, None),
    ({"POST list": "create_no_name"}, OutcomeUnknown, None),
])
def test_failures_map_to_not_sent_or_unknown(clock, vault, route, exc, retry):
    prov = make(clock, vault, Fixtures(**(OK | route)))
    with staged(context(clock)), pytest.raises(exc) as info:
        prov.run(request())
    if retry is not None:
        assert info.value.retryable is retry


def test_calls_to_other_origins_are_refused(clock, vault):
    prov = make(clock, vault, Fixtures(**OK))
    with pytest.raises(TransportError):
        prov._call(RawCall("GET", "https://evil.example/v4/x"))
    with pytest.raises(TransportError):
        prov._call(RawCall("POST", "https://oauth2.googleapis.com/other"))


def test_reconcile_matches_exact_text_after_the_send(clock, vault):
    prov = make(clock, vault, Fixtures(**OK))
    rb = c.ReadbackRequest(tenant_id=T, idempotency_key=KEY, publication_id="p1")
    with staged(context(clock)):
        out = prov.run(rb)
    assert out.external_ids["external_id"].endswith("/p-1") and out.output.live_text == TEXT
    prov2 = make(clock, vault, Fixtures(**(OK | {"GET list": "list_twice"})))
    with staged(context(clock)), pytest.raises(AmbiguousLookup):
        prov2.run(rb)


def test_readback_of_a_missing_post_has_no_text(clock, vault):
    prov = make(clock, vault, Fixtures(**(OK | {"GET post": "get_missing"})))
    with staged(context(clock)):
        assert prov.run(READBACK).output.live_text is None


def test_no_reply_or_profile_endpoint_exists():
    """Module 27's guard: the only paths built are localPosts create, get and list."""
    src = Path(gbp.__file__).read_text()
    for word in ("updateReply", "/reply", "reviews", "questions", "answers", "updateMask", "PATCH"):
        assert word not in src.split('"""', 2)[2], word
    assert set(c.REQUEST_TYPES[Capability.PUBLISHING]) == {c.PublishRequest, c.ReadbackRequest}
