"""Deterministic mock for the messaging capability. Wraps the contract mock: no outside calls, no cost."""
from __future__ import annotations

from cortex_operator.connections.registry import ProviderContext, ProviderRegistry
from cortex_operator.contracts.enums import Capability, ConnectionMode
from cortex_operator.contracts.testing.mocks import MockProvider

CAPABILITY = Capability.MESSAGING


def create(ctx: ProviderContext) -> MockProvider:
    return MockProvider(CAPABILITY, ctx.clock, name="mock")


def register(registry: ProviderRegistry) -> None:
    registry.register(CAPABILITY, "mock", ConnectionMode.MOCK, create)
