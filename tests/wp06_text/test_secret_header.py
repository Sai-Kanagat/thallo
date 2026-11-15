"""A stored secret that cannot be a header value (a trailing newline from copy-paste, CR LF
injection, NUL, non-ASCII) is refused before any connection, and no error path echoes it: not
the message, the args, the repr, a chained cause, the traceback with its locals, or a log line."""
from __future__ import annotations

import http.client
import logging
import traceback

import pytest

from thallo.connections import ProviderContext
from thallo.connections.secret import Secret
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.models import Connection
from thallo.contracts.testing.samples import T, _id
from thallo.providers.text import claude, gemini, openai_compatible
from thallo.providers.text.base import AddressPolicy, Auth, HttpCall, HttpTransport, TextProviderError

from .conftest import ALLOWED_ORIGIN, FixtureTransport

BASE = "fake-key-for-test"
BAD = {
    "trailing newline": BASE + "\n",
    "crlf injection": BASE + "\r\nx-injected: yes",
    "nul": BASE + "\x00tail",
    "non-ascii": BASE + "é",
    "leading space": " " + BASE,
    "trailing tab": BASE + "\t",
}
PRICES = {"input_usd_per_mtok": "1", "output_usd_per_mtok": "1"}


def catch(fn, *args) -> BaseException:
    """Runs fn and returns what it raised. This harness frame, whose args are the test's input, is
    dropped from the traceback, so `capture_locals` sees only the code under test."""
    try:
        fn(*args)
    except TextProviderError as exc:
        return exc.with_traceback(exc.__traceback__.tb_next)  # drop this harness frame
    raise AssertionError("expected TextProviderError")


def leaks(exc: BaseException, secret: str, logs: str = "") -> list[str]:
    """Every place the secret, or its first-line prefix with the base, shows up."""
    needles = {secret, secret.strip(), BASE}
    te = traceback.TracebackException.from_exception(exc, capture_locals=True)
    places = {
        "str": str(exc), "repr": repr(exc), "args": repr(exc.args),
        "format_exception": "".join(traceback.format_exception(exc, chain=True)),
        "capture_locals": "".join(te.format(chain=True)),
        "cause": repr(exc.__cause__) + repr(exc.__context__),
        "logs": logs,
    }
    return [where for where, text in places.items() if any(n in text for n in needles)]


class RecordingFactory:
    """A real http.client connection whose socket is never opened; records any attempt."""

    def __init__(self) -> None:
        self.opened = 0

    def __call__(self, scheme, host, port, ip, timeout):
        self.opened += 1
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)

        def refuse() -> None:
            raise AssertionError("a connection was attempted")
        conn.connect = refuse  # type: ignore[method-assign]
        return conn


class Resolver:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, host, port):
        self.calls += 1
        return ["93.184.216.34"]


@pytest.mark.parametrize("kind", list(BAD))
@pytest.mark.parametrize("header,scheme", [("authorization", "Bearer "), ("x-api-key", ""),
                                           ("x-goog-api-key", "")])
def test_transport_refuses_before_connecting(kind, header, scheme, caplog):
    caplog.set_level(logging.DEBUG)
    secret = BAD[kind]
    factory, resolver = RecordingFactory(), Resolver()
    t = HttpTransport(AddressPolicy.PUBLIC, resolver=resolver, connect=factory)
    call = HttpCall("POST", "https://api.vendor.invalid/v1/x", {}, {"a": 1}, Auth(header, Secret(secret), scheme))
    exc = catch(t, call)
    assert factory.opened == 0 and resolver.calls == 0
    assert leaks(exc, secret, caplog.text) == []
    assert "length" not in str(exc) and str(len(secret)) not in str(exc)


class RaisingConnection:
    """A connection whose request fails the way http.client does, quoting the header bytes."""

    def __init__(self, exc_type: type[Exception]) -> None:
        self.exc_type = exc_type

    def factory(self, scheme, host, port, ip, timeout):
        return self

    def request(self, method, path, body=None, headers=None):
        raise self.exc_type(f"Invalid header value {headers!r}".encode())

    def close(self) -> None:
        pass


@pytest.mark.parametrize("exc_type", [ValueError, TypeError])
def test_value_and_type_errors_are_not_chained(exc_type, caplog):
    caplog.set_level(logging.DEBUG)
    conn = RaisingConnection(exc_type)
    t = HttpTransport(AddressPolicy.PUBLIC, resolver=Resolver(), connect=conn.factory)
    call = HttpCall("POST", "https://api.vendor.invalid/v1/x", {}, None, Auth("authorization", Secret(BASE), "Bearer "))
    exc = catch(t, call)
    assert exc.__cause__ is None and exc.__context__ is None
    assert leaks(exc, BASE, caplog.text) == []


class StubVault:
    """Hands back a secret the real vault would now refuse, as an old row could."""

    def __init__(self, value: str) -> None:
        self.value = value

    def resolve(self, tenant_id, ref):
        return Secret(self.value)


def ctx(provider: str, settings: dict, secret: str, clock) -> ProviderContext:
    row = Connection(id=_id(91), created_at=clock.now(), tenant_id=T, capability=Capability.TEXT,
                     provider=provider, mode=ConnectionMode.HOSTED, model="some-model", settings=settings,
                     secret_ref="sec_" + "0" * 32)
    return ProviderContext(T, row, clock, StubVault(secret))  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", list(BAD))
@pytest.mark.parametrize("module,settings", [
    (claude, PRICES), (gemini, PRICES),
    (openai_compatible, {**PRICES, "base_url": ALLOWED_ORIGIN + "/v1"}),
])
def test_providers_refuse_before_the_transport(kind, module, settings, clock, caplog):
    caplog.set_level(logging.DEBUG)
    secret = BAD[kind]
    transport = FixtureTransport()
    provider = module.create(ctx(module.NAME, settings, secret, clock), transport)
    health = provider.health()
    assert not health.connected and transport.calls == []
    assert BASE not in (health.detail or "")
    exc = catch(provider._auth)
    assert transport.calls == []
    assert leaks(exc, secret, caplog.text) == []
