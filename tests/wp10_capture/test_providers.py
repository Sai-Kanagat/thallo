"""Typed (manual), local and hosted transcription against hand-written fixtures and the contract
suite. No keys, no network: sockets raise, every reply comes from fixtures.json."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from thallo.connections import InMemorySecretStore, KeyRing, ProviderContext, ProviderUnavailable, Vault
from thallo.connections.registry import ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, ConnectionMode, Language
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import T, _id
from thallo.providers.text.base import HttpReply
from thallo.providers.transcription import hosted, local, manual
from thallo.providers.transcription.audio import tenant_audio_reader
from thallo.providers.transcription.base import TranscriptionError, multipart

from .conftest import WEBM

FIX = json.loads((Path(__file__).parent / "fixtures.json").read_text())
KEY = "fake-transcription-key-for-tests"
SHA = hashlib.sha256(WEBM).hexdigest()
ORIGIN = "https://api.example.invalid"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


class Fixtures:
    def __init__(self, post: str = "json_ok", get: str = "models") -> None:
        self.post, self.get, self.calls = post, get, []

    def __call__(self, call):
        self.calls.append(call)
        f = FIX[self.post if call.method == "POST" else self.get]
        return HttpReply(f["status"], json.loads(json.dumps(f["body"])))


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock):
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


def ctx(clock, vault, provider, *, settings, mode, secret=True, model="whisper-1", tenant=T):
    ref = vault.put(tenant, KEY) if secret else None
    row = Connection(id=_id(91), created_at=clock.now(), tenant_id=tenant, capability=Capability.TRANSCRIPTION,
                     provider=provider, mode=mode, model=model, settings=settings, secret_ref=ref)
    return ProviderContext(tenant, row, clock, vault)


def audio(tenant, sha):
    assert tenant == T
    if sha != SHA:
        raise LookupError("not here")
    return WEBM


def req(**kw) -> c.TranscriptionRequest:
    return c.TranscriptionRequest(tenant_id=T, media_sha256=SHA, language_hint=Language.EN,
                                  idempotency_key="transcribe:1", **kw)


def make_hosted(clock, vault, transport=None, **kw):
    settings = kw.pop("settings", {"usd_per_minute": "0.006", "base_url": f"{ORIGIN}/v1"})
    return hosted.create(ctx(clock, vault, "hosted", settings=settings, mode=ConnectionMode.HOSTED, **kw),
                         transport=transport or Fixtures(), audio=audio,
                         environ=kw.pop("environ", {hosted.ORIGINS_ENV: ORIGIN}))


def make_local(clock, vault, transport=None, environ=None):
    return local.create(ctx(clock, vault, "local", settings={}, mode=ConnectionMode.LOCAL, secret=False),
                        transport=transport or Fixtures("verbose_ok"), audio=audio,
                        environ=environ if environ is not None else {local.TENANTS_ENV: T})


def test_all_three_pass_the_contract(clock, vault):
    typed = manual.create(ctx(clock, vault, "manual", settings={}, mode=ConnectionMode.MANUAL, secret=False))
    assert_provider_contract(typed, clock=clock, requests=[req(), req(typed_text="typed by a person")])
    assert_provider_contract(make_hosted(clock, vault), clock=clock, requests=[req()])
    assert_provider_contract(make_local(clock, vault), clock=clock, requests=[req()])


def test_registry_discovers_local_and_hosted():
    names = ProviderRegistry.with_discovered().names(Capability.TRANSCRIPTION)
    assert {"manual", "mock", "local", "hosted"} <= set(names)


def test_hosted_sends_audio_and_bearer_to_the_allowed_origin_only(clock, vault):
    t = Fixtures()
    p = make_hosted(clock, vault, t)
    out = p.run(req())
    assert out.output.text.startswith("We delivered 12 cars") and out.output.language is Language.EN
    call = t.calls[-1]
    assert call.url == f"{ORIGIN}/v1/audio/transcriptions"
    assert call.auth.secret.reveal() == KEY and KEY not in call.url and KEY.encode() not in call.body
    assert WEBM in call.body and b'name="language"\r\n\r\nen' in call.body and b"prompt" not in call.body
    assert p.describe().sends_externally


def test_hosted_on_an_origin_the_operator_did_not_allow_makes_no_call(clock, vault):
    t = Fixtures()
    p = make_hosted(clock, vault, t, settings={"usd_per_minute": "0.006", "base_url": "https://evil.example.invalid/v1"})
    assert p.problem() == "the endpoint is not one the operator allowed"
    with pytest.raises(ProviderUnavailable):
        p.run(req())
    assert t.calls == [] and p.health().connected is False


def test_local_reads_verbose_json_and_charges_nothing(clock, vault):
    out = make_local(clock, vault).run(req())
    assert out.output.language is Language.IT and len(out.output.segments) == 2
    assert out.cost_eur == 0 and not make_local(clock, vault).describe().sends_externally


def test_local_needs_the_operator_to_enable_the_tenant(clock, vault):
    p = make_local(clock, vault, environ={})
    assert "not enabled" in p.problem()
    p = make_local(clock, vault, environ={local.TENANTS_ENV: "*", local.URL_ENV: "http://10.0.0.5:8080/v1"})
    assert "loopback" in p.problem()


def test_cost_comes_from_reported_duration(clock, vault):
    p = make_hosted(clock, vault, Fixtures("verbose_ok"))
    assert p.run(req()).cost_eur > 0


def test_a_recording_that_changed_on_disk_is_not_sent(clock, vault):
    t = Fixtures()
    p = hosted.create(ctx(clock, vault, "hosted", settings={"usd_per_minute": "0", "base_url": f"{ORIGIN}/v1"},
                          mode=ConnectionMode.HOSTED), transport=t, audio=lambda tn, sha: WEBM + b"x",
                      environ={hosted.ORIGINS_ENV: ORIGIN})
    with pytest.raises(ProviderUnavailable):
        p.run(req())
    assert t.calls == []


def test_vendor_errors_carry_no_body(clock, vault):
    for name in ("server_error", "bad_shape"):
        with pytest.raises(TranscriptionError) as exc:
            make_hosted(clock, vault, Fixtures(name)).run(req())
        assert "hand-written" not in str(exc.value)


def test_other_tenant_and_no_audio_reader(clock, vault):
    p = make_hosted(clock, vault)
    with pytest.raises(Exception):  # noqa: B017
        p.run(c.TranscriptionRequest(tenant_id=_id(5), media_sha256=SHA, idempotency_key="x"))
    bare = hosted.create(ctx(clock, vault, "hosted", settings={"usd_per_minute": "0", "base_url": f"{ORIGIN}/v1"},
                             mode=ConnectionMode.HOSTED), transport=Fixtures(), environ={hosted.ORIGINS_ENV: ORIGIN})
    assert "recordings" in (bare.problem() or "")


def test_audio_reader_stays_in_the_tenant_folder(tmp_path):
    folder = tmp_path / T.lower()
    folder.mkdir()
    (folder / f"capture-{SHA}").write_bytes(WEBM)
    read = tenant_audio_reader(tmp_path)
    assert read(T, SHA) == WEBM
    for bad in ((T, "../" + SHA), ("not-an-id", SHA), (_id(4), SHA)):
        with pytest.raises(LookupError):
            read(*bad)
    (folder / f"capture-{'a' * 64}").symlink_to(folder / f"capture-{SHA}")
    with pytest.raises(LookupError):
        read(T, "a" * 64)


def test_multipart_boundary_never_appears_in_the_body():
    body, ctype = multipart({"model": "m"}, "file", "a.webm", "audio/webm", WEBM)
    boundary = ctype.split("boundary=")[1]
    assert body.count(boundary.encode()) == 3 and boundary.encode() not in WEBM
