"""The vault: secrets encrypted at rest with AES-256-GCM.

- The master key comes from the environment variable THALLO_VAULT_KEY (base64 of 32 bytes) and
  nowhere else. For rotation, THALLO_VAULT_KEY_PREVIOUS may hold older keys, comma separated.
- Rows elsewhere hold only a `secret_ref`. Plaintext exists only inside a `Secret`.
- Every encryption uses a fresh random 96-bit nonce.
- Tenant, ref and a version counter are bound as associated data, so a ciphertext copied to
  another tenant or ref, or with its version edited, fails to decrypt. The store only accepts a
  replacement whose version is exactly the stored one plus one, so an old row cannot be written back.
- Rotation: put the new key in THALLO_VAULT_KEY, move the old one to THALLO_VAULT_KEY_PREVIOUS,
  call `rewrap_all()`, then drop the old key once `stale_count()` is zero.

Traceback hygiene: plaintext and key bytes are never bound to a local name on a path that can raise.
They are passed inline into the cipher call, or held inside a `Secret` or `AESGCM` object whose
repr shows nothing.
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
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from thallo.connections.errors import (
    SecretNotFound,
    SecretUnreadable,
    VaultError,
    VaultKeyError,
)
from thallo.connections.secret import Secret
from thallo.connections.stores import SealedSecret, SecretStore
from thallo.contracts.clock import Clock

log = logging.getLogger(__name__)

KEY_ENV = "THALLO_VAULT_KEY"
PREVIOUS_KEYS_ENV = "THALLO_VAULT_KEY_PREVIOUS"
MAX_SECRET_BYTES = 64 * 1024
_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_AAD_PREFIX = b"thallo-vault/v2"


def _decode_key(text: str, source: str) -> bytes:
    if not text.strip():
        raise VaultKeyError(f"{source} is not set")
    try:
        return _check_len(base64.b64decode(text.strip() + "=" * (-len(text.strip()) % 4), altchars=b"-_",
                                           validate=True), source)
    except (binascii.Error, ValueError):
        raise VaultKeyError(f"{source} is not valid base64") from None


def _check_len(key: bytes, source: str) -> bytes:
    if len(key) != 32:
        raise VaultKeyError(f"{source} must decode to exactly 32 bytes")
    return key


def _key_id(key: bytes) -> str:
    return hmac.new(key, b"thallo-vault-key-id", hashlib.sha256).hexdigest()[:16]


class KeyRing:
    """The current master key plus any previous ones still needed to read old ciphertext.

    Keys are handed out only as ready `AESGCM` objects, never as bytes."""

    def __init__(self, current: bytes, previous: Sequence[bytes] = ()) -> None:
        self._aeads: dict[str, AESGCM] = {}
        for k in (current, *previous):
            self._aeads[_key_id(_check_len(k, "a master key"))] = AESGCM(k)
        self.current_id = _key_id(current)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> KeyRing:
        env = os.environ if environ is None else environ
        previous = [_decode_key(p, PREVIOUS_KEYS_ENV) for p in env.get(PREVIOUS_KEYS_ENV, "").split(",")
                    if p.strip()]
        return cls(_decode_key(env.get(KEY_ENV, ""), KEY_ENV), previous)

    def aead(self, key_id: str) -> AESGCM | None:
        return self._aeads.get(key_id)

    def current_aead(self) -> AESGCM:
        return self._aeads[self.current_id]

    def __repr__(self) -> str:
        return f"KeyRing(keys={len(self._aeads)})"

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise TypeError("a KeyRing cannot be pickled")


def _aad(tenant_id: str, ref: str, version: int) -> bytes:
    t, r = tenant_id.encode(), ref.encode()
    return _AAD_PREFIX + struct.pack(">H", len(t)) + t + struct.pack(">H", len(r)) + r + struct.pack(">Q", version)


def _wrap(value: str | Secret) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


def _clean(secret: Secret) -> bool:
    """Not empty, no control character (C0 or DEL) and no leading or trailing space. A trailing
    newline from copy-paste is the usual cause. Checked on the bytes, never raises."""
    return (secret.size() > 0 and not any(b < 0x20 or b == 0x7F for b in secret.reveal_bytes())
            and secret.reveal_bytes()[:1] != b" " and secret.reveal_bytes()[-1:] != b" ")


def _check_value(secret: Secret) -> None:
    """Refuse, never strip: a vault that silently changes a secret stores something the person
    did not give it. The message names the rule, never the value or its length."""
    if not _clean(secret):
        raise VaultError("a secret may not be empty, hold control characters (such as a newline) "
                         "or start or end with a space; paste it again without them")


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
        """Encrypt and store a new secret. Returns the `secret_ref` to put on a row.
        Pass a `Secret` where you can: a plain str stays alive in the caller's frame."""
        secret = _wrap(value)
        del value
        _check_value(secret)
        ref = "sec_" + os.urandom(16).hex() if ref is None else ref
        self._check(tenant_id, ref)
        if not self._store.put(self._seal(tenant_id, ref, secret, None, 1), replace=False):
            raise VaultError(f"secret {ref} already exists for this tenant")
        log.info("vault put tenant=%s ref=%s", tenant_id, ref)
        return ref

    def replace(self, tenant_id: str, ref: str, value: str | Secret) -> None:
        secret = _wrap(value)
        del value
        _check_value(secret)
        self._check(tenant_id, ref)
        old = self._store.get(tenant_id, ref)
        if old is None:
            raise SecretNotFound(f"no secret {ref} for this tenant")
        if not self._store.put(self._seal(tenant_id, ref, secret, old.created_at, old.version + 1), replace=True):
            raise VaultError(f"secret {ref} changed while it was being replaced")
        log.info("vault replace tenant=%s ref=%s", tenant_id, ref)

    def delete(self, tenant_id: str, ref: str) -> bool:
        self._check(tenant_id, ref)
        removed = self._store.delete(tenant_id, ref)
        log.info("vault delete tenant=%s ref=%s removed=%s", tenant_id, ref, removed)
        return removed

    # ------------------------------------------------------------------ reads

    def exists(self, tenant_id: str, ref: str) -> bool:
        self._check(tenant_id, ref)
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

    def _tenants(self, tenant_ids: Iterable[str] | None) -> list[str]:
        return list(self._store.tenant_ids() if tenant_ids is None else tenant_ids)

    def stale_count(self, tenant_ids: Iterable[str] | None = None) -> int:
        return sum(1 for t in self._tenants(tenant_ids) for s in self._store.list_for_tenant(t)
                   if s.key_id != self._keys.current_id)

    def rewrap_all(self, tenant_ids: Iterable[str] | None = None) -> int:
        """Re-encrypt every secret not under the current key, bumping its version. Returns how many.
        Stores that cannot list every tenant (Postgres under row-level security) need `tenant_ids`."""
        n = 0
        for tenant_id in self._tenants(tenant_ids):
            for s in self._store.list_for_tenant(tenant_id):
                if s.key_id == self._keys.current_id:
                    continue
                fresh = self._seal(s.tenant_id, s.ref, Secret(self._open(s, s.tenant_id, s.ref)),
                                   s.created_at, s.version + 1)
                if not self._store.put(fresh, replace=True):
                    raise VaultError(f"secret {s.ref} changed while it was being rewrapped")
                n += 1
        log.info("vault rewrap count=%d", n)
        return n

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _check(tenant_id: str, ref: str) -> None:
        if not tenant_id or not _REF_RE.match(ref):
            raise VaultError("invalid tenant or secret ref")

    def _seal(self, tenant_id: str, ref: str, secret: Secret, created_at: datetime | None,
              version: int) -> SealedSecret:
        if not 0 < secret.size() <= MAX_SECRET_BYTES:
            raise VaultError("a secret must be 1 byte to 64 KiB")
        nonce = os.urandom(12)
        now = self._clock.now()
        return SealedSecret(
            tenant_id=tenant_id, ref=ref, key_id=self._keys.current_id, nonce=nonce, version=version,
            ciphertext=self._keys.current_aead().encrypt(nonce, secret.reveal_bytes(),
                                                         _aad(tenant_id, ref, version)),
            created_at=created_at or now, updated_at=now)

    def _open(self, sealed: SealedSecret, tenant_id: str, ref: str) -> bytes:
        aead = self._keys.aead(sealed.key_id)
        if aead is None:
            raise SecretUnreadable(f"secret {ref} is under a key that is no longer configured")
        try:
            return aead.decrypt(sealed.nonce, sealed.ciphertext, _aad(tenant_id, ref, sealed.version))
        except InvalidTag:
            raise SecretUnreadable(f"secret {ref} cannot be decrypted for this tenant") from None
