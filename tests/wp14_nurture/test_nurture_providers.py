"""Booking and CRM providers: contract suite, hand-written vendor replies (from the docs, not
captured live), the CRM live switch, and no sockets."""
from __future__ import annotations

import json
import os
import socket
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from thallo.connections import InMemorySecretStore, KeyRing, ProviderContext, ProviderUnavailable, Vault
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import T, _id
from thallo.providers.booking import calcom, calendly, slot_list
from thallo.providers.crm import hubspot
from thallo.providers.publishing.postiz_base import RawReply

FAKE = "fake-token-for-tests-only"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, boom)


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock):
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


def ctx(clock, vault, cap, name, settings, secret=FAKE):
    ref = vault.put(T, secret) if secret else None
    row = Connection(id=_id(71), created_at=clock.now(), tenant_id=T, capability=cap, provider=name,
                     mode=ConnectionMode.HOSTED, settings=settings, secret_ref=ref)
    return ProviderContext(T, row, clock, vault)


class Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, call):
        self.calls.append(call)
        return self.replies.pop(0)


def slots_req(action="slots"):
    return c.BookingRequest(tenant_id=T, idempotency_key="b1", action=action, lead_id="l1", kind="callback")


def test_slot_list_offers_opening_hours_only(clock, vault):
    p = slot_list.create(ctx(clock, vault, Capability.BOOKING, "slot_list", {"timezone": "UTC", "hours": {0: [10, 12]},
                                                                             "slot_minutes": 60}, None))
    assert_provider_contract(p, clock, [slots_req()])
    out = p.run(slots_req()).output.slots
    assert [t.hour for t in out][:2] == [11, 10] or all(t.weekday() == 0 for t in out)
    assert all(t > clock.now() + timedelta(hours=1) and t.weekday() == 0 for t in out)


def test_calcom_reads_public_slots_with_the_api_version(clock, vault):
    body = {"status": "success", "data": {"2026-01-06": [{"start": "2026-01-06T09:00:00.000+01:00"},
                                                        {"start": "2026-01-06T10:00:00.000+01:00"}]}}
    t = Transport([RawReply(200, body)] * 10)
    p = calcom.create(ctx(clock, vault, Capability.BOOKING, "calcom",
                          {"username": "moto", "event_slug": "visita", "event_type_id": 42}, None), transport=t)
    assert_provider_contract(p, clock, [slots_req()])
    out = p.run(slots_req()).output
    assert out.link == "https://cal.com/moto/visita" and len(out.slots) == 2
    call = t.calls[-1]
    assert call.method == "GET" and call.url.startswith(calcom.SLOTS_URL)
    assert call.auth.header == "cal-api-version" and call.auth.secret.reveal() == "2024-09-04"
    assert parse_qs(urlsplit(call.url).query)["eventTypeId"] == ["42"] and "l1" not in call.url


def test_calendly_reads_times_and_falls_back_to_the_link(clock, vault):
    body = {"collection": [{"status": "available", "start_time": "2026-01-06T09:00:00Z"},
                           {"status": "unavailable", "start_time": "2026-01-06T10:00:00Z"}]}
    t = Transport([RawReply(200, body)] * 10 + [RawReply(500, None)])
    p = calendly.create(ctx(clock, vault, Capability.BOOKING, "calendly",
                            {"user_slug": "moto", "event_slug": "visita", "event_type_uuid": "abcd-1234-ef"}),
                        transport=t)
    assert_provider_contract(p, clock, [slots_req()])
    t.replies = [RawReply(200, body)]
    assert len(p.run(slots_req()).output.slots) == 1
    t.replies = [RawReply(500, None)]
    res = p.run(slots_req())
    assert res.output.link == "https://calendly.com/moto/visita" and res.output.slots == () and res.task


def _crm_req(records):
    return c.CrmSyncRequest(tenant_id=T, idempotency_key="crm:1", records=tuple(records))


def test_hubspot_is_off_without_the_operator_switch(clock, vault):
    p = hubspot.create(ctx(clock, vault, Capability.CRM, "hubspot", {}), environ={})
    with pytest.raises(ProviderUnavailable):
        p.estimate(_crm_req([{"thallo_id": "p1"}]))
    assert not p.health().connected


def test_hubspot_creates_updates_and_erases_allowlisted_fields(clock, vault):
    t = Transport([RawReply(201, {"id": "501"}), RawReply(200, {"id": "77"}), RawReply(200, {"id": "88"})] * 3)
    p = hubspot.create(ctx(clock, vault, Capability.CRM, "hubspot", {}), transport=t,
                       environ={hubspot.LIVE_ENV: "1"})
    recs = [{"thallo_id": "p1", "name": "Marco Rossi", "email": "", "phone": "+39333", "stage": "booked",
             "won_amount": "0", "erase": False, "external_id": None},
            {"thallo_id": "p2", "name": "Anna", "email": "a@b.it", "phone": "", "stage": "won", "won_amount": "9",
             "erase": False, "external_id": "77"},
            {"thallo_id": "p3", "name": "", "email": "", "phone": "", "stage": "", "won_amount": "0", "erase": True,
             "external_id": "88"}]
    assert_provider_contract(p, clock, [_crm_req(recs)])
    t.calls.clear()
    t.replies = [RawReply(201, {"id": "501"}), RawReply(200, {"id": "77"}), RawReply(200, {"id": "88"})]
    out = p.run(_crm_req(recs)).output
    assert out.external_ids == {"p1": "501", "p2": "77", "p3": "88"}
    methods = [(x.method, x.url.rsplit("/", 1)[-1]) for x in t.calls]
    assert methods == [("POST", "contacts"), ("PATCH", "77"), ("PATCH", "88")]
    first = json.loads(t.calls[0].body)["properties"]
    assert first == {"firstname": "Marco", "email": "", "phone": "+39333", "hs_lead_status": "OPEN_DEAL"}
    assert json.loads(t.calls[2].body)["properties"] == {"firstname": "", "email": "", "phone": ""}
    assert t.calls[0].auth.secret.reveal() == FAKE


def test_hubspot_partial_failure_keeps_what_was_confirmed(clock, vault):
    t = Transport([RawReply(201, {"id": "1"}), RawReply(500, None)])
    p = hubspot.create(ctx(clock, vault, Capability.CRM, "hubspot", {}), transport=t, environ={hubspot.LIVE_ENV: "1"})
    out = p.run(_crm_req([{"thallo_id": "a", "name": "x"}, {"thallo_id": "b", "name": "y"}])).output
    assert out.external_ids == {"a": "1"}
    t.replies = [RawReply(500, None)]
    with pytest.raises(hubspot.SyncFailed):
        p.run(_crm_req([{"thallo_id": "a", "name": "x"}]))
