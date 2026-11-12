"""Errors raised by the connections layer. Messages never carry a secret value."""
from __future__ import annotations


class ConnectionsError(Exception):
    pass


class VaultError(ConnectionsError):
    """Base for vault failures. Messages name a tenant and a ref at most, never a value."""


class VaultKeyError(VaultError):
    """The master key is missing or malformed."""


class SecretNotFound(VaultError):
    pass


class SecretUnreadable(VaultError):
    """Decryption failed: wrong key, tampered data, or a ciphertext moved to another tenant or ref."""


class UnknownProvider(ConnectionsError):
    pass


class InvalidConnection(ConnectionsError):
    pass


class ManualRequired(ConnectionsError):
    """The tenant-level manual connection is the floor of every fallback chain and cannot be removed."""


class ProviderUnavailable(ConnectionsError):
    """A provider raises this when it did nothing at all (no send, no spend), so the next
    connection in the chain may be tried safely. Any other error stops the chain, because the
    provider may already have reached a third party."""


class NoConnection(ConnectionsError):
    """Every connection in the chain was skipped or unavailable."""
