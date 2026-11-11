"""The vault: secrets encrypted at rest with AES-256-GCM.

- The master key comes from the environment variable THALLO_VAULT_KEY (base64 of 32 bytes) and
  nowhere else. For rotation, THALLO_VAULT_KEY_PREVIOUS may hold older keys, comma separated.
- Rows elsewhere hold only a `secret_ref`. Plaintext exists only inside a `Secret`.
- Every encryption uses a fresh random 96-bit nonce.
- Tenant and ref are bound as associated data, so a ciphertext copied to another tenant or ref
  fails to decrypt.
- Rotation: put the new key in THALLO_VAULT_KEY, move the old one to THALLO_VAULT_KEY_PREVIOUS,
  call `rewrap_all()`, then drop the old key once `stale_count()` is zero.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import struct
from collections.abc import Mapping, Sequence
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cortex_operator.connections.errors import (
    SecretNotFound,
    SecretUnreadable,
    VaultError,
    VaultKeyError,
)
from cortex_operator.connections.secret import Secret
from cortex_operator.connections.stores import SealedSecret, SecretStore
from cortex_operator.contracts.clock import Clock

log = logging.getLogger(__name__)

KEY_ENV = "THALLO_VAULT_KEY"
PREVIOUS_KEYS_ENV = "THALLO_VAULT_KEY_PREVIOUS"
MAX_SECRET_BYTES = 64 * 1024
_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_AAD_PREFIX = b"thallo-vault/v1"


def _decode_key(text: str, source: str) -> bytes:
    raw = text.strip()
    try:
        key = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=False)
    except (binascii.Error, ValueError):
        raise VaultKeyError(f"{source} is not valid base64") from None
    if len(key) != 32:
        raise VaultKeyError(f"{source} must decode to exactly 32 bytes")
    return key


def _key_id(key: bytes) -> str:
    return hmac.new(key, b"thallo-vault-key-id", hashlib.sha256).hexdigest()[:16]


class KeyRing:
    """The current master key plus any previous ones still needed to read old ciphertext."""

    def __init__(self, current: bytes, previous: Sequence[bytes] = ()) -> None:
        for k in (current, *previous):
            if len(k) != 32:
                raise VaultKeyError("a master key must be exactly 32 bytes")
        self._current = current
        self._by_id = {_key_id(k): k for k in (current, *previous)}
        self.current_id = _key_id(current)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> KeyRing:
        env = os.environ if environ is None else environ
        text = env.get(KEY_ENV, "")
        if not text.strip():
            raise VaultKeyError(f"{KEY_ENV} is not set")
        previous = [_decode_key(p, PREVIOUS_KEYS_ENV) for p in env.get(PREVIOUS_KEYS_ENV, "").split(",") if p.strip()]
        return cls(_decode_key(text, KEY_ENV), previous)

    @property
    def current(self) -> bytes:
        return self._current

    def key_for(self, key_id: str) -> bytes | None:
        return self._by_id.get(key_id)

    def __repr__(self) -> str:
        return f"KeyRing(keys={len(self._by_id)})"

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise TypeError("a KeyRing cannot be pickled")


def _aad(tenant_id: str, ref: str) -> bytes:
    t, r = tenant_id.encode(), ref.encode()
    return _AAD_PREFIX + struct.pack(">H", len(t)) + t + struct.pack(">H", len(r)) + r


class Vault:
    def __init__(self, keyring: KeyRing, store: SecretStore, clock: Clock) -> None:
        self._keys = keyring
        self._store = store
        self._clock = clock

    @classmethod
    def from_env(cls, store: SecretStore, clock: Clock, environ: Mapping[str, str] | None = None) -> Vault:
        return cls(KeyRing.from_env(environ), store, clock)

    def __repr__(self) -> str:
        return "Vault()"

    # ------------------------------------------------------------------ writes

    def put(self, tenant_id: str, value: str | Secret, *, ref: str | None = None) -> str:
        """Encrypt and store a new secret. Returns the `secret_ref` to put on a row."""
        ref = "sec_" + os.urandom(16).hex() if ref is None else ref
        self._check(tenant_id, ref)
        sealed = self._seal(tenant_id, ref, value, created_at=None)
        if not self._store.put(sealed, replace=False):
            raise VaultError(f"secret {ref} already exists for this tenant")
        log.info("vault put tenant=%s ref=%s", tenant_id, ref)
        return ref

    def replace(self, tenant_id: str, ref: str, value: str | Secret) -> None:
        old = self._store.get(tenant_id, ref)
        if old is None:
            raise SecretNotFound(f"no secret {ref} for this tenant")
        self._store.put(self._seal(tenant_id, ref, value, created_at=old.created_at), replace=True)
        log.info("vault replace tenant=%s ref=%s", tenant_id, ref)

    def delete(self, tenant_id: str, ref: str) -> bool:
        removed = self._store.delete(tenant_id, ref)
        log.info("vault delete tenant=%s ref=%s removed=%s", tenant_id, ref, removed)
        return removed

    # ------------------------------------------------------------------ reads

    def exists(self, tenant_id: str, ref: str) -> bool:
        return self._store.get(tenant_id, ref) is not None

    def get(self, tenant_id: str, ref: str) -> Secret:
        self._check(tenant_id, ref)
        sealed = self._store.get(tenant_id, ref)
        if sealed is None:
            raise SecretNotFound(f"no secret {ref} for this tenant")
        return Secret(self._open(sealed, tenant_id, ref))

    def resolve(self, tenant_id: str, secret_ref: str | None) -> Secret | None:
        return None if secret_ref is None else self.get(tenant_id, secret_ref)

    # ------------------------------------------------------------------ rotation

    def stale_count(self) -> int:
        return sum(1 for s in self._store.all() if s.key_id != self._keys.current_id)

    def rewrap_all(self) -> int:
        """Re-encrypt every secret not under the current key. Returns how many were rewrapped."""
        n = 0
        for s in list(self._store.all()):
            if s.key_id == self._keys.current_id:
                continue
            plain = self._open(s, s.tenant_id, s.ref)
            self._store.put(self._seal(s.tenant_id, s.ref, Secret(plain), created_at=s.created_at), replace=True)
            n += 1
        log.info("vault rewrap count=%d", n)
        return n

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _check(tenant_id: str, ref: str) -> None:
        if not tenant_id or not _REF_RE.match(ref):
            raise VaultError("invalid tenant or secret ref")

    def _seal(self, tenant_id: str, ref: str, value: str | Secret, created_at: datetime | None) -> SealedSecret:
        plain = Secret(value).reveal_bytes()
        if not plain or len(plain) > MAX_SECRET_BYTES:
            raise VaultError("a secret must be 1 byte to 64 KiB")
        nonce = os.urandom(12)
        ct = AESGCM(self._keys.current).encrypt(nonce, plain, _aad(tenant_id, ref))
        now = self._clock.now()
        return SealedSecret(tenant_id=tenant_id, ref=ref, key_id=self._keys.current_id, nonce=nonce,
                            ciphertext=ct, created_at=created_at or now, updated_at=now)

    def _open(self, sealed: SealedSecret, tenant_id: str, ref: str) -> bytes:
        key = self._keys.key_for(sealed.key_id)
        if key is None:
            raise SecretUnreadable(f"secret {ref} is under a key that is no longer configured")
        try:
            return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, _aad(tenant_id, ref))
        except InvalidTag:
            raise SecretUnreadable(f"secret {ref} cannot be decrypted for this tenant") from None
