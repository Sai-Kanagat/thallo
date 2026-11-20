"""Calendar providers: the contract suite on hand-written fixtures, upsert by slot id, the
dedicated-calendar guard, credentials only where they belong, manual fallthrough. Sockets raise."""
from __future__ import annotations

import json
import os
import socket

import pytest

from thallo.connections import InMemorySecretStore, KeyRing, ProviderContext, ProviderUnavailable, Vault
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import T, _id
from thallo.providers.calendar import google_calendar, manual, notion_calendar
from thallo.providers.calendar.http_base import MirrorFailed
from thallo.providers.publishing.postiz_base import RawReply
from thallo.providers.text.base import HttpReply

GOOGLE_CRED = json.dumps({"client_id": "cid", "client_secret": "fake-secret", "refresh_token": "fake-refresh"})
CAL = "abc123thallo@group.calendar.google.com"
DB = "0123456789abcdef0123456789abcdef"
SLOT_ID = "0190f000-0000-7000-8000-000000000001"
SLOT = {"slot_id": SLOT_ID, "title": "instagram: Deliveries", "starts_at": "2026-01-12T09:00:00+00:00",
        "ends_at": "2026-01-12T09:30:00+00:00", "details": "Stage: consideration"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock):
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


def ctx(clock, vault, name, settings, secret):
    ref = vault.put(T, secret) if secret else None
    row = Connection(id=_id(81), created_at=clock.now(), tenant_id=T, capability=Capability.CALENDAR,
                     provider=name, mode=ConnectionMode.HOSTED, settings=settings, secret_ref=ref)
    return ProviderContext(T, row, clock, vault)


def req(*slots):
    return c.CalendarRequest(tenant_id=T, slots=slots, idempotency_key="k")


class GoogleFake:
    """Recorded-style answers written from the Calendar v3 reference."""

    def __init__(self, summary="Thallo plan", exists=False, marker=SLOT_ID):
        self.summary, self.exists, self.marker, self.calls = summary, exists, marker, []

    def __call__(self, call):
        self.calls.append(call)
        if call.url.startswith("https://oauth2.googleapis.com/token"):
            return RawReply(200, {"access_token": "ya29.fake", "expires_in": 3599, "token_type": "Bearer"})
        if call.method == "GET" and "/events/" not in call.url:
            return RawReply(200, {"kind": "calendar#calendar", "id": CAL, "summary": self.summary})
        if call.method == "POST":
            return RawReply(409 if self.exists else 200, {"id": "x"})
        if call.method == "GET":
            return RawReply(200, {"id": "x", "extendedProperties": {"private": {"thallo_slot": self.marker}}})
        return RawReply(200, {"id": "x"})


class NotionFake:
    def __init__(self, hits=0):
        self.hits, self.calls = hits, []

    def __call__(self, call):
        self.calls.append(call)
        if call.url.endswith("/query"):
            return HttpReply(200, {"object": "list", "results": [{"object": "page", "id": f"p{i}"}
                                                                  for i in range(self.hits)]})
        return HttpReply(200, {"object": "page", "id": "new"})


def google(clock, vault, fake, cal=CAL):
    return google_calendar.create(ctx(clock, vault, "google_calendar", {"calendar_id": cal}, GOOGLE_CRED),
                                  transport=fake)


def notion(clock, vault, fake):
    return notion_calendar.create(ctx(clock, vault, "notion_calendar", {"database_id": DB}, "secret_fake"),
                                  transport=fake)


def test_contract(clock, vault):
    assert_provider_contract(google(clock, vault, GoogleFake()), clock, requests=[req(SLOT)])
    assert_provider_contract(google(clock, vault, GoogleFake()), clock)  # malformed sample slot is skipped
    assert_provider_contract(notion(clock, vault, NotionFake()), clock, requests=[req(SLOT)])
    assert_provider_contract(notion(clock, vault, NotionFake()), clock)
    m = manual.create(ctx(clock, vault, "manual", {}, None))
    assert_provider_contract(m, clock, requests=[req(SLOT)])


def test_google_writes_one_event_with_marker_and_no_notifications(clock, vault):
    fake = GoogleFake()
    out = google(clock, vault, fake).run(req(SLOT, {"day": "mon"})).output
    assert out.mirrored == 1 and out.comments_in == ()
    post = next(c for c in fake.calls if c.method == "POST" and "/events" in c.url)
    body = json.loads(post.body)
    assert "sendUpdates=none" in post.url and "attendees" not in body
    assert body["id"] == "thallo" + SLOT_ID.replace("-", "")
    assert body["extendedProperties"]["private"]["thallo_slot"] == SLOT_ID
    assert fake.calls[0].auth is None or "fake-secret" not in fake.calls[0].auth.secret.reveal()
    assert all(c.auth.secret.reveal() == "ya29.fake" for c in fake.calls[1:])


def test_google_updates_own_event_and_refuses_others(clock, vault):
    fake = GoogleFake(exists=True)
    assert google(clock, vault, fake).run(req(SLOT)).output.mirrored == 1
    assert any(c.method == "PUT" for c in fake.calls)
    foreign = GoogleFake(exists=True, marker=None)
    with pytest.raises(MirrorFailed, match="left untouched"):
        google(clock, vault, foreign).run(req(SLOT))
    assert not any(c.method == "PUT" for c in foreign.calls)


def test_google_never_writes_the_owners_calendar(clock, vault):
    for bad in ("primary", "owner@gmail.com"):
        p = google(clock, vault, GoogleFake(), cal=bad)
        with pytest.raises(ProviderUnavailable):
            p.run(req(SLOT))
    shared = GoogleFake(summary="Family")
    with pytest.raises(MirrorFailed, match="dedicated"):
        google(clock, vault, shared).run(req(SLOT))
    assert not any("/events" in c.url for c in shared.calls)


def test_notion_upserts_by_slot(clock, vault):
    fresh = NotionFake()
    assert notion(clock, vault, fresh).run(req(SLOT)).output.mirrored == 1
    assert [c.method for c in fresh.calls] == ["POST", "POST"]
    assert fresh.calls[0].headers["notion-version"] == "2022-06-28"
    assert fresh.calls[0].body["filter"]["rich_text"]["equals"] == SLOT_ID
    again = NotionFake(hits=1)
    notion(clock, vault, again).run(req(SLOT))
    assert again.calls[1].method == "PATCH" and again.calls[1].url.endswith("/pages/p0")
    with pytest.raises(MirrorFailed, match="same Thallo slot"):
        notion(clock, vault, NotionFake(hits=2)).run(req(SLOT))


def test_unconfigured_falls_through(clock, vault):
    p = notion_calendar.create(ctx(clock, vault, "notion_calendar", {"database_id": DB}, None))
    with pytest.raises(ProviderUnavailable):
        p.estimate(req(SLOT))
