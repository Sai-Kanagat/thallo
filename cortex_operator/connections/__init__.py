"""Connections: the registry, the vault and the health check (WP-02).

    providers = ProviderRegistry.with_discovered()
    registry = ConnectionRegistry(clock=clock, providers=providers, connections=store, vault=vault)
    choice = registry.resolve(tenant_id, Capability.MESSAGING)
    served = registry.run(tenant_id, request)
"""
from cortex_operator.connections.errors import (
    ConnectionsError,
    InvalidConnection,
    ManualRequired,
    NoConnection,
    ProviderUnavailable,
    SecretNotFound,
    SecretUnreadable,
    UnknownProvider,
    VaultError,
    VaultKeyError,
)
from cortex_operator.connections.health import HealthMonitor, HealthReport
from cortex_operator.connections.registry import (
    ConnectionRegistry,
    ProviderContext,
    ProviderRegistry,
    Served,
)
from cortex_operator.connections.secret import Secret
from cortex_operator.connections.stores import (
    InMemoryChannelAccountStore,
    InMemoryConnectionStore,
    InMemoryHealthState,
    InMemorySecretStore,
    ListEventSink,
)
from cortex_operator.connections.vault import KeyRing, Vault

__all__ = [
    "ConnectionRegistry", "ConnectionsError", "HealthMonitor", "HealthReport", "InMemoryChannelAccountStore",
    "InMemoryConnectionStore", "InMemoryHealthState", "InMemorySecretStore", "InvalidConnection", "KeyRing",
    "ListEventSink", "ManualRequired", "NoConnection", "ProviderContext", "ProviderRegistry",
    "ProviderUnavailable", "Secret", "SecretNotFound", "SecretUnreadable", "Served", "UnknownProvider",
    "Vault", "VaultError", "VaultKeyError",
]
