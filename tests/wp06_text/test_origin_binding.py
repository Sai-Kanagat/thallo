"""Security fix 3 (HANDOFF, 14 Nov): a tenant-writable base_url must never decide where a text
provider's key goes. Claude and Gemini send only to a constant origin; the OpenAI-compatible
provider only to an origin the operator lists in THALLO_OPENAI_COMPATIBLE_ORIGINS. Every hostile
endpoint below must make the provider unavailable with no connection attempted at all."""
from __future__ import annotations

import pytest

from thallo.connections import ConnectionRegistry, InMemoryConnectionStore, ProviderRegistry
from thallo.connections.errors import InvalidConnection, ProviderUnavailable
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, Language, TextRole
from thallo.contracts.models import Connection
from thallo.contracts.testing.samples import T, _id
from thallo.providers.text import claude, gemini, openai_compatible
from thallo.providers.text.base import AddressPolicy, HttpCall, HttpTransport, TextProviderError, parse_origin

from .conftest import FAKE_KEY, FixtureTransport

PRICES = {"input_usd_per_mtok": "1", "output_usd_per_mtok": "2"}
ALLOWED = {openai_compatible.ORIGINS_ENV: "https://api.allowed.com"}

HOSTILE = [
    "https://evil.example/v1",  # another host
    "https://api.allowed.com:8443/v1",  # same host, another port
    "http://api.allowed.com/v1",  # plain http
    "https://api.allowed.com@evil.example/",  # userinfo: the host is evil.example
    "https://api.allowed.com:443@evil.example/v1",
    "https://api.allowed.com./v1",  # trailing dot
    "https://API.Allowed.com/v1",  # mixed case
    "https://api.allowed.com.evil.example/v1",  # prefix match
    "https://evil.example/https://api.allowed.com/v1",  # substring match
    "https://evil.example\\@api.allowed.com/v1",  # backslash
    "https://api.allowed.com%2F@evil.example/v1",
    "https://xn--api-allowed.com/v1",
    "https://api.allowed.com:0443/v1",
]


class Recorder:
    """A real HttpTransport whose resolver and socket factory only record: any call reaching
    either of them is a connection attempt."""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, int]] = []
        self.connected: list[tuple] = []
        self.transport = HttpTransport(AddressPolicy.PUBLIC, resolver=self._resolve, connect=self._connect)

    def _resolve(self, host, port):
        self.resolved.append((host, port))
        return ["93.184.216.34"]

    def _connect(self, *args):
        self.connected.append(args)
        raise AssertionError("a connection was attempted")

    def __call__(self, call: HttpCall):
        return self.transport(call)

    @property
    def attempted(self) -> bool:
        return bool(self.resolved or self.connected)


def request() -> c.TextRequest:
    return c.TextRequest(tenant_id=T, role=TextRole.DRAFT, instructions="Write one line.", untrusted_data=("x",),
                         language=Language.EN, max_output_tokens=50, idempotency_key="k-origin")


def compat(make_ctx, url: str, transport, environ=ALLOWED):
    ctx = make_ctx("openai_compatible", model="gpt-5-mini", settings={**PRICES, "base_url": url})
    return openai_compatible.create(ctx, transport=transport, environ=environ)


def assert_never_called(prov, attempted) -> None:
    """`attempted()` is True once anything reached the transport's network side."""
    assert prov.health().connected is False
    with pytest.raises(ProviderUnavailable):
        prov.estimate(request())
    with pytest.raises(ProviderUnavailable):
        prov.run(request())
    assert not attempted()


# ---------------------------------------------------------------------- OpenAI-compatible

@pytest.mark.parametrize("url", HOSTILE)
def test_hostile_base_url_never_receives_the_key(make_ctx, url):
    rec = Recorder()
    assert_never_called(compat(make_ctx, url, rec), lambda: rec.attempted)
    fx = FixtureTransport()  # records every call handed to the transport at all
    assert_never_called(compat(make_ctx, url, fx), lambda: bool(fx.calls))


@pytest.mark.parametrize("environ", [{}, {openai_compatible.ORIGINS_ENV: ""},
                                     {openai_compatible.ORIGINS_ENV: "https://api.allowed.com/v1"},  # a path
                                     {openai_compatible.ORIGINS_ENV: "https://API.allowed.com"},
                                     {openai_compatible.ORIGINS_ENV: "api.allowed.com"}])
def test_no_or_bad_operator_list_is_unavailable(make_ctx, environ):
    fx = FixtureTransport()
    prov = compat(make_ctx, "https://api.allowed.com/v1", fx, environ=environ)
    assert prov.health().connected is False and "operator" in prov.health().detail
    with pytest.raises(ProviderUnavailable):
        prov.run(request())
    assert fx.calls == []


@pytest.mark.parametrize("url", ["https://api.allowed.com/v1", "https://api.allowed.com:443/v1",
                                 "https://api.allowed.com"])
def test_allowed_origin_is_served(make_ctx, url):
    fx = FixtureTransport("openai_chat", by_method={"GET": "openai_models"})
    prov = compat(make_ctx, url, fx)
    assert prov.problem() is None
    assert prov.run(request()).output.text
    assert [parse_origin(call.url) for call in fx.calls] == [("https", "api.allowed.com", 443)]


def test_one_of_several_operator_origins(make_ctx):
    env = {openai_compatible.ORIGINS_ENV: "https://api.allowed.com, https://other.allowed.com:8443"}
    fx = FixtureTransport("openai_chat")
    assert compat(make_ctx, "https://other.allowed.com:8443/v1", fx, environ=env).run(request())
    rec = Recorder()
    assert_never_called(compat(make_ctx, "https://other.allowed.com/v1", rec, environ=env), lambda: rec.attempted)


def test_send_refuses_a_call_to_another_origin_whatever_built_it(make_ctx):
    """The backstop: even a call built past `problem()` never reaches the transport."""
    fx = FixtureTransport("openai_chat")
    prov = compat(make_ctx, "https://api.allowed.com/v1", fx)
    good = prov.build_call
    prov.build_call = lambda req, auth: HttpCall("POST", "https://evil.example/v1/chat/completions", {},
                                                 good(req, auth).body, auth)
    with pytest.raises(TextProviderError, match="did not allow"):
        prov.run(request())
    assert fx.calls == []


def test_hostile_row_through_the_registry_falls_through_to_manual(make_ctx, clock, vault, monkeypatch):
    """A row written straight into the store (WP-09 makes settings tenant-writable)."""
    monkeypatch.setenv(openai_compatible.ORIGINS_ENV, "https://api.allowed.com")
    rec = Recorder()
    providers = ProviderRegistry.with_discovered()
    providers.register(Capability.TEXT, "openai_compatible", ConnectionMode.HOSTED,
                       lambda ctx: openai_compatible.create(ctx, transport=rec), replace=True,
                       settings_schema=openai_compatible.OpenAICompatibleSettings)
    reg = ConnectionRegistry(clock=clock, providers=providers, connections=InMemoryConnectionStore(), vault=vault,
                             environ={})
    reg.ensure_manual(T, Capability.TEXT)
    row = Connection(id=_id(91), created_at=clock.now(), tenant_id=T, capability=Capability.TEXT,
                     provider="openai_compatible", mode=ConnectionMode.HOSTED, model="gpt-5-mini", priority=0,
                     settings={**PRICES, "base_url": "https://api.allowed.com.evil.example/v1"},
                     secret_ref=vault.put(T, FAKE_KEY))
    reg.connections.put(row)
    served = reg.run(T, request())
    assert served.provider == "manual" and served.skipped == ((row.id, "unavailable"),)
    assert not rec.attempted


# ---------------------------------------------------------------------- Claude and Gemini

@pytest.mark.parametrize(("module", "origin"), [(claude, ("https", "api.anthropic.com", 443)),
                                                 (gemini, ("https", "generativelanguage.googleapis.com", 443))])
def test_fixed_vendors_have_no_base_url_setting(module, origin, make_ctx, clock, vault):
    assert module.ORIGINS == frozenset({origin})
    schema = {"claude": claude.ClaudeSettings, "gemini": gemini.GeminiSettings}[module.NAME]
    assert "base_url" not in schema.model_fields
    with pytest.raises(ValueError):
        schema.model_validate({**PRICES, "base_url": "https://evil.example"})
    reg = ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                             connections=InMemoryConnectionStore(), vault=vault, environ={})
    with pytest.raises(InvalidConnection):
        reg.add(T, Capability.TEXT, module.NAME, model="m", settings={**PRICES, "base_url": "https://evil.example"})


@pytest.mark.parametrize("module", [claude, gemini])
@pytest.mark.parametrize("url", ["https://evil.example", "https://api.anthropic.com.evil.example"])
def test_a_stored_base_url_on_a_fixed_vendor_is_never_used(module, url, make_ctx):
    rec = Recorder()
    ctx = make_ctx(module.NAME, model="m-1", settings={**PRICES, "base_url": url})
    prov = module.create(ctx, transport=rec)
    assert_never_called(prov, lambda: rec.attempted)
    assert "settings" in prov.health().detail


@pytest.mark.parametrize("module", [claude, gemini])
def test_fixed_vendor_calls_go_only_to_its_constant_origin(module, make_ctx):
    fx = FixtureTransport({"claude": "claude_messages", "gemini": "gemini_generate"}[module.NAME])
    ctx = make_ctx(module.NAME, model="m-1", settings=PRICES)
    module.create(ctx, transport=fx).run(request())
    assert {parse_origin(call.url) for call in fx.calls} == module.ORIGINS


# ---------------------------------------------------------------------- the parser

@pytest.mark.parametrize("url", HOSTILE[3:] + ["https://", "https://:443", "HTTPS://api.allowed.com",
                                               "https://api.allowed.com:99999", "https://a..b.com",
                                               "https://api.allowed.com\t/v1", "https://ápi.allowed.com"])
def test_parse_origin_refuses_or_differs(url):
    try:
        got = parse_origin(url)
    except ValueError:
        return
    assert got != ("https", "api.allowed.com", 443)


def test_parse_origin_ipv6_and_ports():
    assert parse_origin("https://[2606:2800:220:1::1]:8443/v1") == ("https", "2606:2800:220:1::1", 8443)
    assert parse_origin("http://127.0.0.1:11434", schemes=("http", "https")) == ("http", "127.0.0.1", 11434)
    with pytest.raises(ValueError):
        parse_origin("http://127.0.0.1:11434")  # https only unless the caller allows http


def test_transport_checks_are_unchanged():
    """The connect-time address check still runs on an allowed origin (the other transport
    tests cover redirects and the pinned address)."""
    t = HttpTransport(AddressPolicy.PUBLIC, resolver=lambda h, p: ["10.0.0.1"],
                      connect=lambda *a: (_ for _ in ()).throw(AssertionError("connected")))
    with pytest.raises(TextProviderError, match="outside the public policy"):
        t(HttpCall("GET", "https://api.allowed.com/v1/models"))
