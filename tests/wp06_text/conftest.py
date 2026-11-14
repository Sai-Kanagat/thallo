"""Fixtures for WP-06. No keys, no network: every vendor reply comes from fixtures.json, which is
hand-written from the vendors' documented shapes (see its _provenance line), and sockets raise."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from thallo.connections import InMemorySecretStore, KeyRing, ProviderContext, Vault
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, ConnectionMode, TextRole
from thallo.contracts.models import Connection
from thallo.contracts.testing.samples import T, _id
from thallo.providers.text.base import HttpCall, HttpReply

FIXTURES = json.loads((Path(__file__).parent / "fixtures.json").read_text())
FAKE_KEY = "fake-key-for-tests-only"  # not a real key; never sent anywhere


def reply(name: str) -> HttpReply:
    f = FIXTURES[name]
    return HttpReply(f["status"], json.loads(json.dumps(f["body"])))


class FixtureTransport:
    """Replays named fixtures in order and records every call it was given."""

    def __init__(self, *names: str, by_method: dict[str, str] | None = None) -> None:
        self.queue = list(names)
        self.by_method = by_method or {}
        self.calls: list[HttpCall] = []

    def __call__(self, call: HttpCall) -> HttpReply:
        self.calls.append(call)
        if call.method in self.by_method:
            return reply(self.by_method[call.method])
        if not self.queue:
            raise AssertionError("no fixture left for this call")
        return reply(self.queue.pop(0))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock) -> Vault:
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


@pytest.fixture
def make_ctx(clock, vault):
    def _make(provider: str, *, model: str | None, settings: dict, mode=ConnectionMode.HOSTED, secret: bool = True,
              tenant: str = T, role: TextRole | None = None) -> ProviderContext:
        ref = vault.put(tenant, FAKE_KEY) if secret else None
        row = Connection(id=_id(90), created_at=clock.now(), tenant_id=tenant, capability=Capability.TEXT,
                         provider=provider, mode=mode, model=model, role=role, settings=settings, secret_ref=ref)
        return ProviderContext(tenant, row, clock, vault)
    return _make
