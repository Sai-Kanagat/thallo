"""Deterministic mock for the storage capability. Wraps the contract mock: no outside calls, no cost."""
from __future__ import annotations

from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider

CAPABILITY = Capability.STORAGE


def create(ctx: ProviderContext) -> MockProvider:
    return MockProvider(CAPABILITY, ctx.clock, name="mock")


def register(registry: ProviderRegistry) -> None:
    registry.register(CAPABILITY, "mock", ConnectionMode.MOCK, create, settings_schema=NoSettings)
