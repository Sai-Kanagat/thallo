"""The media providers against hand-written fixtures. No network: every call goes to a fake transport."""
from __future__ import annotations

import base64
import socket
from decimal import Decimal

import pytest

from thallo.connections import ProviderContext, ProviderUnavailable
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.providers.image import generated_hosted, generated_local, stock
from thallo.providers.image.http_media import NO_PEOPLE, MediaReply
from thallo.providers.video import generated_hosted as video_gen

from .conftest import T, consent, png

API = "https://api.example.com"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


class FakeVault:
    def resolve(self, tenant_id, ref):  # noqa: ANN001, ANN201
        from thallo.connections.secret import Secret

        return Secret("test-key-not-real")


class Recorder:
    def __init__(self, replies):  # noqa: ANN001
        self.replies, self.calls = replies, []

    def __call__(self, call):  # noqa: ANN001, ANN204
        self.calls.append(call)
        for prefix, reply in self.replies:
            if call.url.startswith(prefix) or prefix in call.url:
                return reply
        return MediaReply(200, {"data": []})


def ctx(cap, name, mode, clock, model="m-1", secret=True):
    row = Connection(id="0190f3a0-0000-7000-8000-0000000000f1", created_at=clock.now(), tenant_id=T, capability=cap,
                     provider=name, mode=mode, model=model, settings={"usd_per_unit": "0.04"},
                     secret_ref="0190f3a0-0000-7000-8000-0000000000f2" if secret else None)
    return ProviderContext(T, row, clock, FakeVault() if secret else None)


def img_reply():
    return MediaReply(200, {"data": [{"b64_json": base64.b64encode(png()).decode()}]})


def bg(**kw):
    return c.ImageRequest(tenant_id=T, purpose="background", prompt="soft paper texture", width=64, height=64,
                          idempotency_key="k-bg", **kw)


def hosted_env(env, switch=True):
    e = dict(env, THALLO_IMAGE_GEN_ORIGINS=API, THALLO_IMAGE_GEN_ORIGINS_BASE=f"{API}/v1")
    if switch:
        e["THALLO_ALLOW_GENERATED_MEDIA"] = "1"
    return e


def test_hosted_image_passes_the_contract_behind_the_switch(clock, env):
    t = Recorder([("/images/generations", img_reply()), ("/models", MediaReply(200, {}))])
    p = generated_hosted.create(ctx(Capability.IMAGE, "generated_hosted", ConnectionMode.HOSTED, clock), t,
                                hosted_env(env))
    assert p.health().connected
    assert_provider_contract(p, clock, [bg()])
    res = p.run(bg())
    assert res.output.disclosure_required and res.cost_eur > 0 and res.provenance.model == "m-1"
    assert t.calls[-1].auth is not None


def test_switch_off_means_unavailable_and_no_call(clock, env):
    t = Recorder([])
    p = generated_hosted.create(ctx(Capability.IMAGE, "generated_hosted", ConnectionMode.HOSTED, clock), t,
                                hosted_env(env, switch=False))
    assert not p.health().connected and "switched off" in p.health().detail
    with pytest.raises(ProviderUnavailable):
        p.estimate(bg())
    assert t.calls == []


@pytest.mark.parametrize("purpose", ["upload", "template_render"])
def test_generated_never_serves_uploads_or_templates(clock, env, purpose):
    p = generated_hosted.create(ctx(Capability.IMAGE, "generated_hosted", ConnectionMode.HOSTED, clock),
                                Recorder([]), hosted_env(env))
    with pytest.raises(ProviderUnavailable):
        p.estimate(c.ImageRequest(tenant_id=T, purpose=purpose, template_ref="quote-01", width=9, height=9))


def _prompt(p):  # noqa: ANN001
    import json

    p.run(bg(allow_generated_people=True, consent_id="0190f3a0-0000-7000-8000-0000000000c1"))
    return json.loads(p.transport.calls[-1].body)["prompt"]


def test_no_people_guard_without_a_verified_consent(clock, env):
    def make(lookup):  # noqa: ANN001
        return generated_hosted.create(ctx(Capability.IMAGE, "generated_hosted", ConnectionMode.HOSTED, clock),
                                       Recorder([("/images/generations", img_reply())]), hosted_env(env), lookup)
    assert NO_PEOPLE in _prompt(make(None))
    live = consent(clock)
    assert NO_PEOPLE not in _prompt(make(lambda t, i: live))
    assert NO_PEOPLE in _prompt(make(lambda t, i: consent(clock, withdrawn=-1)))
    assert NO_PEOPLE in _prompt(make(lambda t, i: consent(clock, given=3)))
    assert NO_PEOPLE in _prompt(make(lambda t, i: consent(clock, tenant="0190f3a0-0000-7000-8000-000000000002")))


def test_local_image_is_loopback_only(clock, env):
    e = dict(env, THALLO_ALLOW_GENERATED_MEDIA="1", THALLO_IMAGE_LOCAL_URL="http://127.0.0.1:7860/v1")
    t = Recorder([("/images/generations", img_reply()), ("/models", MediaReply(200, {}))])
    p = generated_local.create(ctx(Capability.IMAGE, "generated_local", ConnectionMode.LOCAL, clock, secret=False),
                               t, e)
    assert_provider_contract(p, clock, [bg()])
    assert not p.describe().sends_externally and all(call.auth is None for call in t.calls)
    bad = generated_local.create(ctx(Capability.IMAGE, "generated_local", ConnectionMode.LOCAL, clock, secret=False),
                                 t, dict(e, THALLO_IMAGE_LOCAL_URL="http://10.0.0.5/v1"))
    assert not bad.health().connected


def test_stock_records_licence_and_sends_no_key_to_the_cdn(clock, env):
    cdn = "https://images.example.com"
    search = MediaReply(200, {"photos": [{"id": 42, "url": "https://www.pexels.com/photo/42", "photographer": "Ana",
                                          "photographer_url": "https://www.pexels.com/@ana", "alt": "olive tree",
                                          "src": {"large2x": f"{cdn}/42.png"}}]})
    t = Recorder([("/search", search), (cdn, MediaReply(200, None, png(), "image/png")),
                  ("/curated", MediaReply(200, {}))])
    e = dict(env, THALLO_STOCK_ORIGINS=f"{API},{cdn}", THALLO_STOCK_ORIGINS_BASE=f"{API}/v1")
    p = stock.create(ctx(Capability.IMAGE, "stock", ConnectionMode.HOSTED, clock, model=None), t, e)
    req = bg().model_copy(update={"purpose": "illustration", "prompt": "olive tree"})
    assert_provider_contract(p, clock, [req])
    res = p.run(req)
    s = res.provenance.settings
    assert "Pexels" in s["licence"] and s["photographer"] == "Ana" and res.external_ids == {"external_id": "42"}
    to_cdn = [x for x in t.calls if x.url.startswith(cdn)]
    assert to_cdn and all(x.auth is None for x in to_cdn)
    assert any(x.auth is not None for x in t.calls if "/search" in x.url)


def test_stock_refuses_an_origin_the_operator_did_not_allow(clock, env):
    search = MediaReply(200, {"photos": [{"id": 1, "src": {"large2x": "https://evil.example.net/x.png"}}]})
    e = dict(env, THALLO_STOCK_ORIGINS=API, THALLO_STOCK_ORIGINS_BASE=f"{API}/v1")
    p = stock.create(ctx(Capability.IMAGE, "stock", ConnectionMode.HOSTED, clock, model=None),
                     Recorder([("/search", search)]), e)
    with pytest.raises(Exception, match="did not allow"):
        p.run(bg().model_copy(update={"purpose": "illustration"}))


def test_generated_video_polls_and_downloads(clock, env):
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
    t = Recorder([("/videos/v_1/content", MediaReply(200, None, mp4, "video/mp4")),
                  ("/videos/v_1", MediaReply(200, {"status": "completed"})),
                  ("/videos", MediaReply(200, {"id": "v_1", "status": "queued"})), ("/models", MediaReply(200, {}))])
    e = dict(env, THALLO_ALLOW_GENERATED_MEDIA="1", THALLO_VIDEO_GEN_ORIGINS=API,
             THALLO_VIDEO_GEN_ORIGINS_BASE=f"{API}/v1")
    p = video_gen.HostedVideoGenerator(ctx(Capability.VIDEO, "generated_hosted", ConnectionMode.HOSTED, clock),
                                       transport=t, environ=e, sleep=lambda s: None)
    req = c.VideoRequest(tenant_id=T, action="generate", script="a slow pan over paper", width=64, height=64,
                         duration_ms=4000, idempotency_key="k-v")
    assert_provider_contract(p, clock, [req])
    res = p.run(req)
    assert res.cost_eur > Decimal(0)
    assert res.output.disclosure_required and res.external_ids == {"external_id": "v_1"}
    assert NO_PEOPLE in res.provenance.settings["prompt_sent"]
