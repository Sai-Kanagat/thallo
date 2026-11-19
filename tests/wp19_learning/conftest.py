"""WP-19 fixtures. No keys, no network: model graders run through the real connection registry with a
provider that replays recorded replies from evals/fixtures/grader_replies.json."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from thallo.connections import (
    ConnectionRegistry,
    InMemoryConnectionStore,
    InMemorySecretStore,
    KeyRing,
    ProviderRegistry,
    Vault,
)
from thallo.connections.registry import NoSettings
from thallo.contracts import connection as c
from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.ids import new_id
from thallo.contracts.models import Edit
from thallo.contracts.testing.mocks import MockProvider

T1 = "0190f3a0-0000-7000-8000-000000000001"
T2 = "0190f3a0-0000-7000-8000-000000000002"
USER = "0190f3a0-0000-7000-8000-0000000000aa"
FIXTURE = Path(__file__).resolve().parents[2] / "evals/fixtures/grader_replies.json"
REPLIES = json.loads(FIXTURE.read_text())["replies"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


class Recorded(MockProvider):
    def run(self, request: c.Request) -> c.RunResult:
        base = super().run(request)
        piece = request.untrusted_data[0] if isinstance(request, c.TextRequest) and request.untrusted_data else ""
        text = next((v for k, v in REPLIES.items() if piece.startswith(k)), "")
        return base.model_copy(update={"output": c.TextResult(text=text)})


@pytest.fixture
def registry(clock):
    providers = ProviderRegistry.with_discovered()
    providers.register(Capability.TEXT, "recorded", ConnectionMode.MOCK,
                       lambda ctx: Recorded(Capability.TEXT, ctx.clock, name="recorded"), settings_schema=NoSettings)
    vault = Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)
    reg = ConnectionRegistry(clock=clock, providers=providers, connections=InMemoryConnectionStore(), vault=vault,
                             mock_tenants={T1, T2}, environ={})
    for t in (T1, T2):
        reg.add(t, Capability.TEXT, "recorded")
    return reg


class Verifier:
    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id == "live"


@pytest.fixture
def verifier():
    return Verifier()


@pytest.fixture
def human():
    return HumanActor(id=USER, tenant_id=T1, session_id="live")


def make_edit(clock, before: str, after: str, tenant: str = T1) -> Edit:
    return Edit(id=new_id(clock), tenant_id=tenant, created_at=clock.now(), variant_id=new_id(clock),
                editor_id=USER, before_text=before, after_text=after)


