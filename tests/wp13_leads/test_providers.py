"""Messaging providers: manual, WhatsApp Cloud, Resend and SMTP pass the contract; the live ones
send only with the operator switch on and a person on the request. Replies are hand-written from
the vendors' docs (not captured from live servers); sockets raise."""
from __future__ import annotations

import json
import os
import smtplib
import socket

import pytest

from thallo.connections import InMemorySecretStore, KeyRing, ProviderContext, ProviderUnavailable, Vault
from thallo.contracts import connection as c
from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, ConnectionMode, MessageChannel
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import T, _id
from thallo.providers.messaging import manual, resend, smtp, whatsapp_cloud
from thallo.providers.messaging.live_base import LIVE_ENV, MessageNotSent, MessageOutcomeUnknown
from thallo.providers.publishing.postiz_base import RawReply, TransportError

FAKE_KEY = "fake-messaging-credential-for-tests-only"
LIVE = {LIVE_ENV: "1", smtp.ORIGIN_ENV: "smtps://mail.example.com"}
HUMAN = HumanActor(id=_id(40), tenant_id=T, session_id="s")


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


def ctx(clock, vault, name, settings, secret=FAKE_KEY):
    ref = vault.put(T, secret) if secret else None
    row = Connection(id=_id(70), created_at=clock.now(), tenant_id=T, capability=Capability.MESSAGING, provider=name,
                     mode=ConnectionMode.HOSTED, settings=settings, secret_ref=ref)
    return ProviderContext(T, row, clock, vault)


def req(channel=MessageChannel.WHATSAPP, to="+39 333 123 4567", human=HUMAN, **kw):
    base = dict(tenant_id=T, idempotency_key="message:1", message_id=_id(21), channel=channel, to=to,
                body="Ciao Marco, quando passi?", consent_id=_id(23), human=human)
    return c.MessageSendRequest(**(base | kw))


class Http:
    def __init__(self, reply=None, raises=None):
        self.reply, self.raises, self.calls = reply, raises, []

    def __call__(self, call):
        self.calls.append(call)
        if self.raises:
            raise self.raises
        return self.reply


WA_OK = RawReply(200, {"messaging_product": "whatsapp", "contacts": [{"input": "393331234567", "wa_id": "393331234567"}],
                       "messages": [{"id": "wamid.HBgM", "message_status": "accepted"}]})
RS_OK = RawReply(200, {"id": "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"})


def wa(clock, vault, http, environ=LIVE, settings=None):
    return whatsapp_cloud.create(ctx(clock, vault, "whatsapp_cloud", settings or {"phone_number_id": "1234567890"}),
                                 transport=http, environ=environ)


def rs(clock, vault, http, environ=LIVE):
    return resend.create(ctx(clock, vault, "resend", {"from_address": "shop@example.com", "from_name": "Moto"}),
                         transport=http, environ=environ)


class FakeSmtp:
    def __init__(self, fail=None):
        self.fail, self.sent, self.logins, self.opened = fail, [], [], []

    def __call__(self, host, ip, port, timeout):
        self.opened.append((host, ip, port))
        return self

    def login(self, user, pw):
        self.logins.append((user, pw))

    def send_message(self, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)
        return {}

    def quit(self):
        pass


def sm(clock, vault, fake, environ=LIVE):
    return smtp.create(ctx(clock, vault, "smtp", {"login": "shop", "from_address": "shop@example.com"}),
                       transport=fake, environ=environ, resolver=lambda h, p: ["93.184.216.34"])


# ---------------------------------------------------------------- contract

def test_manual_passes_the_contract(clock):
    row = Connection(id=_id(71), created_at=clock.now(), tenant_id=T, capability=Capability.MESSAGING,
                     provider="manual", mode=ConnectionMode.MANUAL)
    assert_provider_contract(manual.create(ProviderContext(T, row, clock)), clock, requests=[req()])


def test_live_providers_pass_the_contract(clock, vault):
    assert_provider_contract(wa(clock, vault, Http(WA_OK)), clock, requests=[req()])
    assert_provider_contract(rs(clock, vault, Http(RS_OK)), clock, requests=[req(MessageChannel.EMAIL, "a@b.it")])
    assert_provider_contract(sm(clock, vault, FakeSmtp()), clock, requests=[req(MessageChannel.EMAIL, "a@b.it")])
    for p in (wa(clock, vault, Http(WA_OK)), rs(clock, vault, Http(RS_OK))):
        assert p.describe().sends_externally and p.health().connected


# ---------------------------------------------------------------- the three locks

@pytest.mark.parametrize("make", ["wa", "rs", "sm"])
def test_switch_off_or_no_person_sends_nothing(clock, vault, make):
    http, fake = Http(WA_OK), FakeSmtp()
    build = {"wa": lambda env: wa(clock, vault, http, env), "rs": lambda env: rs(clock, vault, http, env),
             "sm": lambda env: sm(clock, vault, fake, env)}[make]
    channel = MessageChannel.WHATSAPP if make == "wa" else MessageChannel.EMAIL
    to = "+393331234567" if make == "wa" else "a@b.it"
    off = build({smtp.ORIGIN_ENV: "smtps://mail.example.com"})
    assert not off.health().connected
    with pytest.raises(ProviderUnavailable, match="live sending is off"):
        off.run(req(channel, to))
    template_only = req(channel, to, human=None, template_approval_id=_id(22))
    with pytest.raises(ProviderUnavailable, match="person"):
        build(LIVE).run(template_only)
    assert http.calls == [] and fake.sent == [] and fake.opened == []


def test_unconfigured_is_unavailable(clock, vault):
    p = whatsapp_cloud.create(ctx(clock, vault, "whatsapp_cloud", {}, secret=None), environ=LIVE)
    assert not p.health().connected
    with pytest.raises(ProviderUnavailable):
        p.estimate(req())
    s = smtp.create(ctx(clock, vault, "smtp", {"login": "x", "from_address": "a@b.it"}), environ={LIVE_ENV: "1"})
    assert "THALLO_SMTP_ORIGIN" in s.health().detail


def test_settings_cannot_name_a_host():
    for model in (whatsapp_cloud.WhatsAppSettings, resend.EmailSettings, smtp.SmtpSettings):
        assert not {"host", "url", "origin", "base_url"} & set(model.model_fields)
    with pytest.raises(ValueError):
        smtp.operator_origin("smtp://mail.example.com")
    with pytest.raises(ValueError):
        smtp.operator_origin("smtps://u:p@mail.example.com")
    assert smtp.operator_origin(" smtps://mail.example.com:2465\n") == ("mail.example.com", 2465)


# ---------------------------------------------------------------- what goes out

def test_whatsapp_body_and_auth(clock, vault):
    http = Http(WA_OK)
    out = wa(clock, vault, http).run(req())
    assert out.output.status == "sent" and out.output.external_id == "wamid.HBgM"
    (call,) = http.calls
    assert call.url == "https://graph.facebook.com/v21.0/1234567890/messages"
    body = json.loads(call.body)
    assert body == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": "393331234567",
                    "type": "text", "text": {"preview_url": False, "body": "Ciao Marco, quando passi?"}}
    assert call.auth.scheme == "Bearer " and call.auth.secret.reveal() == FAKE_KEY


def test_resend_body(clock, vault):
    http = Http(RS_OK)
    rs(clock, vault, http).run(req(MessageChannel.EMAIL, "anna@acme.it"))
    body = json.loads(http.calls[0].body)
    assert http.calls[0].url == "https://api.resend.com/emails"
    assert body == {"from": "Moto <shop@example.com>", "to": ["anna@acme.it"], "subject": "Your enquiry",
                    "text": "Ciao Marco, quando passi?"}


@pytest.mark.parametrize(("reply", "raises", "error"), [
    (RawReply(400, {"error": "x"}), None, MessageNotSent),
    (RawReply(500, None), None, MessageOutcomeUnknown),
    (RawReply(200, {"messages": []}), None, MessageOutcomeUnknown),
    (None, TransportError("t", reached=True), MessageOutcomeUnknown),
    (None, TransportError("t", reached=False), MessageNotSent),
])
def test_failures_say_not_sent_or_unknown(clock, vault, reply, raises, error):
    with pytest.raises(error):
        wa(clock, vault, Http(reply, raises)).run(req())


def test_smtp_pins_the_checked_address_and_maps_failures(clock, vault):
    fake = FakeSmtp()
    out = sm(clock, vault, fake).run(req(MessageChannel.EMAIL, "anna@acme.it"))
    assert fake.opened == [("mail.example.com", "93.184.216.34", 465)]
    assert fake.logins == [("shop", FAKE_KEY)] and fake.sent[0]["To"] == "anna@acme.it"
    assert out.output.external_id.startswith("<")
    with pytest.raises(MessageNotSent):
        sm(clock, vault, FakeSmtp(smtplib.SMTPRecipientsRefused({}))).run(req(MessageChannel.EMAIL, "a@b.it"))
    with pytest.raises(MessageOutcomeUnknown):
        sm(clock, vault, FakeSmtp(smtplib.SMTPServerDisconnected())).run(req(MessageChannel.EMAIL, "a@b.it"))
    private = smtp.create(ctx(clock, vault, "smtp", {"login": "s", "from_address": "a@b.it"}), transport=FakeSmtp(),
                          environ=LIVE, resolver=lambda h, p: ["10.0.0.5"])
    with pytest.raises(MessageNotSent, match="not public"):
        private.run(req(MessageChannel.EMAIL, "a@b.it"))
