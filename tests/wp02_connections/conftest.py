from __future__ import annotations

import base64
import os

import pytest

from thallo.connections import (
    ConnectionRegistry,
    InMemoryConnectionStore,
    InMemorySecretStore,
    KeyRing,
    ProviderRegistry,
    Vault,
)
from thallo.contracts.clock import FAKE_EPOCH, FakeClock

T1 = "0190f3a0-0000-7000-8000-000000000001"
T2 = "0190f3a0-0000-7000-8000-000000000002"


def new_key() -> bytes:
    """Test keys are generated inside the tests, never stored."""
    return os.urandom(32)


def b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def key():
    return new_key()


@pytest.fixture
def secret_store():
    return InMemorySecretStore()


@pytest.fixture
def vault(key, secret_store, clock):
    return Vault(KeyRing(key), secret_store, clock)


@pytest.fixture
def providers():
    return ProviderRegistry.with_discovered()


@pytest.fixture
def registry(clock, providers, vault):
    """T1 is a demo tenant (mocks allowed); T2 is a real one."""
    return ConnectionRegistry(clock=clock, providers=providers, connections=InMemoryConnectionStore(), vault=vault,
                              mock_tenants={T1}, environ={})
