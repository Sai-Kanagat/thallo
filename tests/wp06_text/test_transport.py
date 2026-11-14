"""The real transport: no redirects, SSRF policy, pinned address, quiet errors. The resolver and
the connection are injected, so nothing here touches DNS or a socket."""
from __future__ import annotations

import json
import ssl

import pytest

from thallo.connections.secret import Secret
from thallo.providers.text import base
from thallo.providers.text.base import (
    AddressPolicy,
    Auth,
    HttpCall,
    HttpTransport,
    TextProviderError,
    address_allowed,
    check_https,
)

from .conftest import FAKE_KEY


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.status, self._body, self.headers = status, body, headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.opened: tuple | None = None
        self.sent: list[tuple] = []
        self.closed = False

    def factory(self, scheme, host, port, ip, timeout):
        self.opened = (scheme, host, port, ip)
        return self

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class CountingResolver:
    def __init__(self, *answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, host, port):
        self.calls += 1
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


def call(url="https://api.vendor.invalid/v1/messages", auth=True) -> HttpCall:
    return HttpCall("POST", url, {"anthropic-version": "x"}, {"a": 1},
                    Auth("x-api-key", Secret(FAKE_KEY)) if auth else None)


def transport(conn, *answers, policy=AddressPolicy.PUBLIC):
    resolver = CountingResolver(*(answers or (["93.184.216.34"],)))
    return HttpTransport(policy, resolver=resolver, connect=conn.factory), resolver


def test_public_call_connects_to_the_validated_address():
    conn = FakeConnection(FakeResponse(200, b'{"ok": true}'))
    t, resolver = transport(conn, ["93.184.216.34"], ["10.0.0.1"])  # a second answer would be private
    rep = t(call())
    assert rep.status == 200 and rep.body == {"ok": True}
    assert conn.opened == ("https", "api.vendor.invalid", 443, "93.184.216.34")
    assert resolver.calls == 1  # resolved once; the socket goes to that address
    method, path, body, headers = conn.sent[0]
    assert (method, path, json.loads(body)) == ("POST", "/v1/messages", {"a": 1})
    assert headers["x-api-key"] == FAKE_KEY and conn.closed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_refused(status):
    conn = FakeConnection(FakeResponse(status, b"", {"location": "https://evil.invalid/steal"}))
    t, _ = transport(conn)
    with pytest.raises(TextProviderError) as err:
        t(call())
    assert "redirect" in str(err.value) and "evil" not in str(err.value)
    assert len(conn.sent) == 1  # one request, never a second one carrying the key


@pytest.mark.parametrize("ip", [
    "10.0.0.1", "172.16.5.4", "192.168.1.1", "127.0.0.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
    "224.0.0.1", "240.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fd00::1", "ff02::1",
    "::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "2002:a00:1::", "64:ff9b::a00:1",
    "not-an-ip",
])
def test_non_public_addresses_refused(ip):
    conn = FakeConnection(FakeResponse(200, b"{}"))
    t, _ = transport(conn, [ip])
    with pytest.raises(TextProviderError) as err:
        t(call())
    assert ip not in str(err.value) and conn.opened is None


def test_one_private_answer_among_public_ones_refuses():
    conn = FakeConnection(FakeResponse(200, b"{}"))
    t, _ = transport(conn, ["93.184.216.34", "10.0.0.1"])
    with pytest.raises(TextProviderError):
        t(call())
    assert conn.opened is None


@pytest.mark.parametrize("url", ["http://api.vendor.invalid/v1", "file:///etc/hosts", "https://u:p@x.invalid/",
                                 "https://x.invalid/#frag", "ftp://x.invalid/"])
def test_public_policy_needs_plain_https(url):
    conn = FakeConnection(FakeResponse(200, b"{}"))
    t, _ = transport(conn)
    with pytest.raises(TextProviderError):
        t(call(url))
    assert conn.opened is None


def test_loopback_policy():
    conn = FakeConnection(FakeResponse(200, b"{}"))
    t, _ = transport(conn, ["127.0.0.1"], policy=AddressPolicy.LOOPBACK)
    assert t(call("http://localhost:11434/api/chat", auth=False)).status == 200
    conn2 = FakeConnection(FakeResponse(200, b"{}"))
    t2, _ = transport(conn2, ["93.184.216.34"], policy=AddressPolicy.LOOPBACK)
    with pytest.raises(TextProviderError):
        t2(call("http://localhost:11434/api/chat", auth=False))
    assert conn2.opened is None


def test_transport_errors_do_not_echo_bodies_or_addresses():
    class Broken(FakeConnection):
        def getresponse(self):
            raise ConnectionResetError("reset by 10.9.8.7")
    conn = Broken(FakeResponse(200, b""))
    t, _ = transport(conn)
    with pytest.raises(TextProviderError) as err:
        t(call())
    assert "10.9.8.7" not in str(err.value) and FAKE_KEY not in str(err.value)


def test_provider_error_is_a_status_class(make_ctx):
    from thallo.providers.text import claude

    from .test_providers import request
    conn = FakeConnection(FakeResponse(500, b'{"error": "internal at 10.0.0.9", "trace": "secret stuff"}'))
    t, _ = transport(conn)
    ctx = make_ctx("claude", model="claude-sonnet-5-5", settings={"input_usd_per_mtok": 1, "output_usd_per_mtok": 1})
    with pytest.raises(TextProviderError) as err:
        claude.create(ctx, transport=t).run(request())
    assert str(err.value) == "claude answered HTTP 5xx"


def test_pinned_https_verifies_the_original_host(monkeypatch):
    seen = {}

    def fake_connect(addr, timeout):
        seen["addr"] = addr
        return object()

    monkeypatch.setattr(base.socket, "create_connection", fake_connect)
    conn = base.pinned_connection("https", "api.vendor.invalid", 443, "93.184.216.34", 5.0)
    assert conn._tls.verify_mode is ssl.CERT_REQUIRED and conn._tls.check_hostname is True

    def wrap(sock, server_hostname):
        seen["sni"] = server_hostname
        return sock

    monkeypatch.setattr(conn._tls, "wrap_socket", wrap)
    conn.connect()
    assert seen == {"addr": ("93.184.216.34", 443), "sni": "api.vendor.invalid"}
    assert conn.host == "api.vendor.invalid"  # Host header and certificate name


@pytest.mark.parametrize("url", ["https://x.invalid/v1?key=1", "https://x.invalid/v1#a", "https://10.0.0.1/v1",
                                 "https://[::ffff:127.0.0.1]/v1", "https://localhost/v1", "https://box.internal/v1",
                                 "https://169.254.169.254/latest"])
def test_hosted_base_url_static_checks(url):
    with pytest.raises(ValueError):
        check_https(url)


def test_address_policy_accepts_public_ipv4_and_ipv6():
    assert address_allowed("93.184.216.34", AddressPolicy.PUBLIC)
    assert address_allowed("2606:2800:220:1::1", AddressPolicy.PUBLIC)
    assert not address_allowed("93.184.216.34", AddressPolicy.LOOPBACK)
