from __future__ import annotations

import socket
from decimal import Decimal

import pytest

from thallo.connections import ProviderContext, ProviderRegistry
from thallo.connections.errors import ProviderUnavailable
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, ConnectionMode, MessageChannel
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import _id, sample_requests

from .conftest import T1


def _ctx(cap, name, mode, clock):
    row = Connection(id=_id(99), created_at=clock.now(), tenant_id=T1, capability=cap, provider=name, mode=mode)
    return ProviderContext(T1, row, clock)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


_DISCOVERED = ProviderRegistry.with_discovered()
ALL = [(cap, name) for cap in Capability for name in _DISCOVERED.names(cap)]


@pytest.mark.parametrize("cap", list(Capability))
def test_every_capability_has_a_manual_provider(providers, cap):
    assert "manual" in providers.names(cap)


@pytest.mark.parametrize(("cap", "name"), ALL)
def test_provider_passes_contract(providers, cap, name):
    """Parametrised over discovery: a new provider file is covered with no edit here."""
    clock = FakeClock(FAKE_EPOCH)
    entry = providers.get(cap, name)
    prov = entry.factory(_ctx(cap, name, entry.mode, clock))
    if prov.health().connected:
        assert_provider_contract(prov, clock)
    else:
        _assert_reports_unavailable(prov, clock)
    assert prov.describe().mode is entry.mode
    if entry.mode in (ConnectionMode.MANUAL, ConnectionMode.MOCK):
        assert not prov.describe().sends_externally


def _assert_reports_unavailable(prov, clock):
    """A provider built with empty settings may be unusable on this machine or for this tenant
    (no local browser, no brand configured). It must then say so in a way the registry skips:
    health() not connected, and estimate() and run() raise ProviderUnavailable for every sample.
    The manual and mock providers never take this path. Its own package tests the full contract."""
    assert prov.describe().mode not in (ConnectionMode.MANUAL, ConnectionMode.MOCK)
    health = prov.health()
    assert health.connected is False and health.checked_at.tzinfo is not None and health.detail
    for req in sample_requests()[prov.capability]:
        for call in (prov.estimate, prov.run):
            with pytest.raises(ProviderUnavailable):
                call(req)
    wrong = next(r for k, rs in sample_requests().items() if k is not prov.capability for r in rs)
    for call in (prov.estimate, prov.run):
        with pytest.raises(c.CapabilityMismatch):
            call(wrong)
    double = prov.mock()
    assert double.capability is prov.capability and double.describe().mode is ConnectionMode.MOCK


@pytest.mark.parametrize("cap", list(Capability))
def test_manual_is_a_person_never_a_provider(providers, cap):
    clock = FakeClock(FAKE_EPOCH)
    prov = providers.get(cap, "manual").factory(_ctx(cap, "manual", ConnectionMode.MANUAL, clock))
    for req in sample_requests()[cap]:
        res = prov.run(req)
        assert res.provenance.made_by == "person"
        assert res.provenance.mode is ConnectionMode.MANUAL
        assert res.status in ("pending_person", "done")
        assert (res.status == "pending_person") == bool(res.task)
        assert res.cost_eur == Decimal(0) and not res.external_ids


def _run(providers, cap, req):
    clock = FakeClock(FAKE_EPOCH)
    return providers.get(cap, "manual").factory(_ctx(cap, "manual", ConnectionMode.MANUAL, clock)).run(req)


def test_manual_messaging_returns_a_link_and_never_sends(providers):
    req = sample_requests()[Capability.MESSAGING][0].model_copy(
        update={"channel": MessageChannel.WHATSAPP, "to": "+39 333 1234567", "body": "Ciao & grazie"})
    res = _run(providers, Capability.MESSAGING, req)
    assert res.output.status == "manual_link"
    assert res.output.external_id is None
    assert res.output.click_to_chat_url == "https://wa.me/393331234567?text=Ciao%20%26%20grazie"
    assert res.status == "pending_person"


def test_manual_messaging_email_and_bad_address(providers):
    base = sample_requests()[Capability.MESSAGING][0]
    ok = _run(providers, Capability.MESSAGING, base)
    assert ok.output.click_to_chat_url.startswith("mailto:test@example.invalid")
    bad = _run(providers, Capability.MESSAGING, base.model_copy(update={"to": "nobody"}))
    assert bad.output.click_to_chat_url is None and bad.output.status == "manual_link"


def test_manual_publishing_is_a_pending_copy_pack(providers):
    pub, readback = sample_requests()[Capability.PUBLISHING]
    res = _run(providers, Capability.PUBLISHING, pub)
    assert res.output.status == "manual_pending"
    assert res.output.live_url is None and res.output.external_id is None
    assert res.output.copy_pack["text"] == pub.text
    assert res.output.copy_pack["approval_id"] == pub.approval_id
    assert res.output.copy_pack["content_hash"] == pub.content_hash
    rb = _run(providers, Capability.PUBLISHING, readback)
    assert rb.output.live_text is None and rb.status == "pending_person"


def test_manual_ads_is_a_checklist_that_moves_no_money(providers):
    reqs = sample_requests()[Capability.ADS]
    res = _run(providers, Capability.ADS, reqs[0])
    assert res.output.status == "manual_checklist" and res.output.rows
    signed = c.AdsRequest(tenant_id=T1, action=c.AdsAction.APPLY_SIGNED, platform="meta",
                          campaign_external_id="cmp-1", spend_proposal_id=_id(1), approval_id=_id(2),
                          approved_content_hash="0" * 64, to_daily=Decimal("12.50"), currency="EUR")
    out = _run(providers, Capability.ADS, signed)
    assert any("12.50" in r["do"] for r in out.output.rows)
    assert out.status == "pending_person"


def test_manual_analytics_reports_missing_not_zero(providers):
    res = _run(providers, Capability.ANALYTICS, sample_requests()[Capability.ANALYTICS][0])
    assert res.output.rows == () and res.output.missing == ("clicks", "impressions")


def test_manual_transcription_uses_typed_text(providers):
    base = sample_requests()[Capability.TRANSCRIPTION][0]
    typed = _run(providers, Capability.TRANSCRIPTION, base.model_copy(update={"typed_text": "hello"}))
    assert typed.output.text == "hello" and typed.status == "done" and typed.task is None
    assert _run(providers, Capability.TRANSCRIPTION, base).status == "pending_person"


def test_manual_media_is_a_marked_placeholder_not_a_generated_file(providers):
    for cap in (Capability.IMAGE, Capability.VIDEO, Capability.VOICEOVER):
        out = _run(providers, cap, sample_requests()[cap][0]).output
        assert out.storage_uri.startswith("manual://pending/")
        assert not out.disclosure_required


def test_manual_research_is_empty_and_untrusted(providers):
    out = _run(providers, Capability.RESEARCH, sample_requests()[Capability.RESEARCH][0]).output
    assert out.items == () and out.untrusted is True


def test_mock_is_deterministic_and_free(providers):
    clock = FakeClock(FAKE_EPOCH)
    prov = providers.get(Capability.TEXT, "mock").factory(_ctx(Capability.TEXT, "mock", ConnectionMode.MOCK, clock))
    req = sample_requests()[Capability.TEXT][0]
    assert prov.run(req).output == prov.run(req).output
    assert prov.run(req).cost_eur == 0


def test_discover_skips_unknown_packages_and_registry_rejects_duplicates():
    reg = ProviderRegistry()
    reg.discover()
    with pytest.raises(Exception, match="already registered"):
        reg.discover()


def test_manual_media_uses_person_provenance_not_client_clip(providers):
    from thallo.contracts.enums import MediaProvenance as MP
    assert _run(providers, Capability.VOICEOVER, sample_requests()[Capability.VOICEOVER][0]).output.provenance \
        is MP.PERSON_RECORDING
    assert _run(providers, Capability.VIDEO, sample_requests()[Capability.VIDEO][0]).output.provenance \
        is MP.PERSON_FILE
