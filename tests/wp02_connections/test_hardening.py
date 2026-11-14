"""Review round: S4 (nothing sensitive in traceback locals), S5, and the nits."""
from __future__ import annotations

import traceback

import pytest

from thallo.connections import (
    InMemorySecretStore,
    InvalidConnection,
    KeyRing,
    ManualRequired,
    Vault,
    VaultError,
    VaultKeyError,
)
from thallo.contracts.enums import Capability, ConnectionMode

from .conftest import T1, b64, new_key
from .test_registry import TEXT, ids, register

PLAIN = "sk-test-LOCALS-7c1d9e"


def leaked_locals(exc: BaseException, needles: set) -> list[str]:
    """Names of locals, in connections/ frames of the traceback, that hold one of the needles."""
    hits = []
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        if "/connections/" not in frame.f_code.co_filename:
            continue
        for name, value in frame.f_locals.items():
            if isinstance(value, str | bytes | bytearray) and value in needles:
                hits.append(f"{frame.f_code.co_name}.{name}")
    return hits


class Boom(InMemorySecretStore):
    def put(self, sealed, *, replace):
        raise RuntimeError("store down")


def test_put_failure_leaves_no_plaintext_or_key_in_traceback(clock):
    key = new_key()
    vault = Vault(KeyRing(key), Boom(), clock)
    with pytest.raises(RuntimeError) as e:
        vault.put(T1, PLAIN)
    assert leaked_locals(e.value, {PLAIN, PLAIN.encode(), key}) == []


def test_replace_and_rewrap_failure_leave_nothing_in_traceback(clock):
    old, new = new_key(), new_key()
    store = InMemorySecretStore()
    ref = Vault(KeyRing(old), store, clock).put(T1, PLAIN)
    rotated = Vault(KeyRing(new, [old]), store, clock)
    store.put = lambda sealed, *, replace: (_ for _ in ()).throw(RuntimeError("down"))  # noqa: ARG005
    needles = {PLAIN, PLAIN.encode(), old, new}
    with pytest.raises(RuntimeError) as e:
        rotated.rewrap_all([T1])
    assert leaked_locals(e.value, needles) == []
    with pytest.raises(RuntimeError) as e:
        rotated.replace(T1, ref, PLAIN + "x")
    assert leaked_locals(e.value, needles | {PLAIN + "x", (PLAIN + "x").encode()}) == []


def test_bad_previous_key_leaves_the_master_key_out_of_the_traceback(clock):
    key = new_key()
    env = {"THALLO_VAULT_KEY": b64(key), "THALLO_VAULT_KEY_PREVIOUS": "not-a-key"}
    with pytest.raises(VaultKeyError) as e:
        KeyRing.from_env(env)
    assert leaked_locals(e.value, {b64(key), key, b64(key).encode()}) == []


def test_decrypt_failure_leaves_nothing_in_traceback(clock):
    store = InMemorySecretStore()
    key = new_key()
    ref = Vault(KeyRing(key), store, clock).put(T1, PLAIN)
    row = store.get(T1, ref)
    store.rows[(T1, ref)] = type(row)(row.tenant_id, row.ref, row.key_id, row.nonce,
                                      bytes([row.ciphertext[0] ^ 1]) + row.ciphertext[1:], row.created_at,
                                      row.updated_at, row.version)
    with pytest.raises(VaultError) as e:
        Vault(KeyRing(key), store, clock).get(T1, ref)
    assert leaked_locals(e.value, {PLAIN, PLAIN.encode(), key}) == []


# ---------------------------------------------------------------- nits

def test_replace_exists_delete_check_their_arguments(vault):
    ref = vault.put(T1, PLAIN)
    for call in (lambda: vault.replace(T1, "bad ref", "x"), lambda: vault.exists(T1, "bad ref"),
                 lambda: vault.delete(T1, "bad ref"), lambda: vault.replace("", ref, "x"),
                 lambda: vault.exists("", ref), lambda: vault.delete("", ref)):
        with pytest.raises(VaultError):
            call()


def test_key_text_with_stray_characters_is_refused(clock):
    good = b64(new_key())
    for bad in (good[:10] + "$" + good[10:], good[:10] + "\n" + good[10:], good[:10] + " " + good[10:]):
        with pytest.raises(VaultKeyError):
            KeyRing.from_env({"THALLO_VAULT_KEY": bad})
    KeyRing.from_env({"THALLO_VAULT_KEY": f"  {good}\n"})  # surrounding whitespace is fine


def test_set_order_is_all_or_nothing(registry, providers):
    register(providers, "a")
    register(providers, "b")
    a, b = registry.add(T1, TEXT, "a"), registry.add(T1, TEXT, "b")
    floor = next(r for r in registry.connections.list_for_tenant(T1) if r.provider == "manual" and r.capability is TEXT)
    for bad in ([b.id, a.id, floor.id], [b.id, "missing", a.id], [b.id, b.id]):
        with pytest.raises((ManualRequired, InvalidConnection)):
            registry.set_order(T1, bad)
        assert registry.connections.get(T1, a.id).priority == 0
        assert registry.connections.get(T1, b.id).priority == 1
    assert ids(registry, registry.resolve(T1, TEXT)) == ["a", "b", "manual"]


def test_no_vault_secret_when_the_connection_is_invalid(registry, providers, secret_store):
    register(providers, "a")
    with pytest.raises(Exception):  # noqa: B017,PT011
        registry.add(T1, TEXT, "a", secret="sk-test-ORPHAN", priority="not a number")
    assert secret_store.rows == {}


def test_no_vault_secret_when_the_connection_store_fails(registry, providers, secret_store):
    register(providers, "a")

    def boom(_row):
        raise RuntimeError("db down")

    registry.connections.put = boom
    with pytest.raises(RuntimeError):
        registry.add(T1, TEXT, "a", secret="sk-test-ORPHAN")
    assert secret_store.rows == {}


# ---------------------------------------------------------------- S5

def test_every_discovered_provider_passes_the_contract(providers):
    """Parametrised over what discovery finds, so a new provider file is covered with no test edit."""
    from thallo.connections import ProviderContext, ProviderUnavailable
    from thallo.contracts.clock import FAKE_EPOCH, FakeClock
    from thallo.contracts.models import Connection
    from thallo.contracts.testing.contract_suite import assert_provider_contract
    from thallo.contracts.testing.samples import _id, sample_requests

    clock = FakeClock(FAKE_EPOCH)
    for cap in Capability:
        names = providers.names(cap)
        assert "manual" in names, f"{cap.value} has no manual provider"
        for name in names:
            entry = providers.get(cap, name)
            row = Connection(id=_id(98), created_at=clock.now(), tenant_id=T1, capability=cap, provider=name,
                             mode=entry.mode)
            prov = entry.factory(ProviderContext(T1, row, clock))
            from .test_providers import _assert_reports_unavailable, _is_unavailable
            if _is_unavailable(prov, cap):  # must say so in a way the registry skips
                _assert_reports_unavailable(prov, clock)
                continue
            assert_provider_contract(prov, clock)
