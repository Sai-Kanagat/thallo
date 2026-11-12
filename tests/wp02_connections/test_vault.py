from __future__ import annotations

import copy
import json
import logging
import pickle

import pytest
from pydantic import BaseModel

from cortex_operator.connections import (
    InMemorySecretStore,
    KeyRing,
    Secret,
    SecretNotFound,
    SecretUnreadable,
    Vault,
    VaultError,
    VaultKeyError,
)
from cortex_operator.connections.secret import REDACTED
from cortex_operator.connections.stores import SealedSecret

from .conftest import T1, T2, b64, new_key

PLAIN = "sk-test-PLAINTEXT-9f8e7d6c5b4a"


def test_round_trip(vault):
    ref = vault.put(T1, PLAIN)
    assert vault.get(T1, ref).reveal() == PLAIN


def test_store_never_holds_plaintext(vault, secret_store):
    ref = vault.put(T1, PLAIN)
    row = secret_store.get(T1, ref)
    blob = row.nonce + row.ciphertext + repr(row).encode() + str(row.__dict__).replace(
        str(row.nonce), "").encode()
    assert PLAIN.encode() not in row.ciphertext
    assert PLAIN.encode() not in blob


def test_fresh_nonce_each_time(vault, secret_store):
    refs = [vault.put(T1, PLAIN) for _ in range(50)]
    nonces = {secret_store.get(T1, r).nonce for r in refs}
    cts = {secret_store.get(T1, r).ciphertext for r in refs}
    assert len(nonces) == 50 and len(cts) == 50


def test_replace_uses_new_nonce(vault, secret_store):
    ref = vault.put(T1, PLAIN)
    before = secret_store.get(T1, ref).nonce
    vault.replace(T1, ref, "another-value")
    assert secret_store.get(T1, ref).nonce != before
    assert vault.get(T1, ref).reveal() == "another-value"


def test_ciphertext_moved_to_another_tenant_fails(vault, secret_store):
    ref = vault.put(T1, PLAIN)
    row = secret_store.get(T1, ref)
    secret_store.rows[(T2, ref)] = SealedSecret(T2, ref, row.key_id, row.nonce, row.ciphertext, row.created_at,
                                                row.updated_at)
    with pytest.raises(SecretUnreadable):
        vault.get(T2, ref)


def test_ciphertext_moved_to_another_ref_fails(vault, secret_store):
    ref = vault.put(T1, PLAIN)
    row = secret_store.get(T1, ref)
    secret_store.rows[(T1, "other_ref")] = SealedSecret(T1, "other_ref", row.key_id, row.nonce, row.ciphertext,
                                                        row.created_at, row.updated_at)
    with pytest.raises(SecretUnreadable):
        vault.get(T1, "other_ref")


def test_tenant_cannot_read_another_tenants_ref(vault):
    ref = vault.put(T1, PLAIN)
    with pytest.raises(SecretNotFound):
        vault.get(T2, ref)


def test_tampered_ciphertext_and_nonce_fail(vault, secret_store):
    ref = vault.put(T1, PLAIN)
    row = secret_store.get(T1, ref)
    flipped = bytes([row.ciphertext[0] ^ 1]) + row.ciphertext[1:]
    secret_store.rows[(T1, ref)] = SealedSecret(T1, ref, row.key_id, row.nonce, flipped, row.created_at,
                                                row.updated_at)
    with pytest.raises(SecretUnreadable):
        vault.get(T1, ref)


def test_wrong_master_key_fails(vault, secret_store, clock):
    ref = vault.put(T1, PLAIN)
    other = Vault(KeyRing(new_key()), secret_store, clock)
    with pytest.raises(SecretUnreadable):
        other.get(T1, ref)


def test_put_existing_ref_refused(vault):
    ref = vault.put(T1, PLAIN)
    with pytest.raises(VaultError):
        vault.put(T1, "x", ref=ref)


@pytest.mark.parametrize("bad", ["", "a b", "x" * 200, "../etc"])
def test_bad_ref_refused(vault, bad):
    with pytest.raises(VaultError):
        vault.put(T1, PLAIN, ref=bad)


def test_empty_secret_refused(vault):
    with pytest.raises(VaultError):
        vault.put(T1, "")


def test_rotation(clock):
    store = InMemorySecretStore()
    old, new = new_key(), new_key()
    v_old = Vault(KeyRing(old), store, clock)
    refs = [v_old.put(T1, f"{PLAIN}-{i}") for i in range(3)]
    v_new = Vault(KeyRing(new, [old]), store, clock)
    assert v_new.stale_count() == 3
    assert v_new.get(T1, refs[0]).reveal() == f"{PLAIN}-0"  # old key still reads
    assert v_new.rewrap_all() == 3
    assert v_new.stale_count() == 0
    assert v_new.rewrap_all() == 0
    v_only_new = Vault(KeyRing(new), store, clock)  # old key retired
    assert [v_only_new.get(T1, r).reveal() for r in refs] == [f"{PLAIN}-{i}" for i in range(3)]
    with pytest.raises(SecretUnreadable):
        Vault(KeyRing(old), store, clock).get(T1, refs[0])


def test_retired_key_before_rewrap_is_unreadable(clock):
    store = InMemorySecretStore()
    old = new_key()
    ref = Vault(KeyRing(old), store, clock).put(T1, PLAIN)
    with pytest.raises(SecretUnreadable):
        Vault(KeyRing(new_key()), store, clock).get(T1, ref)


# ---------------------------------------------------------------- master key from the environment

def test_key_from_env(clock):
    key = new_key()
    v = Vault.from_env(InMemorySecretStore(), clock, {"THALLO_VAULT_KEY": b64(key)})
    assert v.get(T1, v.put(T1, PLAIN)).reveal() == PLAIN


def test_key_from_process_environment(monkeypatch, clock):
    monkeypatch.setenv("THALLO_VAULT_KEY", b64(new_key()))
    v = Vault.from_env(InMemorySecretStore(), clock)
    assert v.get(T1, v.put(T1, PLAIN)).reveal() == PLAIN


def test_previous_keys_from_env(clock):
    old, new = new_key(), new_key()
    store = InMemorySecretStore()
    ref = Vault(KeyRing(old), store, clock).put(T1, PLAIN)
    env = {"THALLO_VAULT_KEY": b64(new), "THALLO_VAULT_KEY_PREVIOUS": f"{b64(old)}, {b64(new_key())}"}
    assert Vault.from_env(store, clock, env).get(T1, ref).reveal() == PLAIN


def test_missing_key_refused(clock, monkeypatch):
    monkeypatch.delenv("THALLO_VAULT_KEY", raising=False)
    with pytest.raises(VaultKeyError) as e:
        Vault.from_env(InMemorySecretStore(), clock)
    assert "THALLO_VAULT_KEY" in str(e.value)


@pytest.mark.parametrize("bad", ["not base64 !!", b64(b"short"), b64(new_key() + b"x")])
def test_bad_key_refused_without_echoing_it(clock, bad):
    with pytest.raises(VaultKeyError) as e:
        Vault.from_env(InMemorySecretStore(), clock, {"THALLO_VAULT_KEY": bad})
    assert bad not in str(e.value)


def test_no_other_key_source(clock, monkeypatch):
    """A key in some other variable, or passed as a plain string setting, is not picked up."""
    monkeypatch.delenv("THALLO_VAULT_KEY", raising=False)
    monkeypatch.setenv("VAULT_KEY", b64(new_key()))
    monkeypatch.setenv("MASTER_KEY", b64(new_key()))
    with pytest.raises(VaultKeyError):
        Vault.from_env(InMemorySecretStore(), clock)


# ---------------------------------------------------------------- nothing leaks

def test_secret_is_redacted_everywhere():
    s = Secret(PLAIN)
    for text in (repr(s), str(s), f"{s}", f"{s!r}", "%s" % s, f"{s:>40}", repr([s]), repr({"k": s})):  # noqa: UP031,E501
        assert PLAIN not in text
    assert REDACTED in str(s)
    assert s.reveal() == PLAIN


def test_secret_in_pydantic_models_and_json():
    class M(BaseModel):
        token: Secret

    m = M(token=PLAIN)
    for text in (repr(m), str(m), json.dumps(m.model_dump(mode="json")), json.dumps(m.model_dump(), default=str),
                 m.model_dump_json(), repr(m.model_dump())):
        assert PLAIN not in text
    assert m.token.reveal() == PLAIN


def test_secret_cannot_be_pickled_hashed_or_mutated():
    s = Secret(PLAIN)
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with pytest.raises(TypeError):
        hash(s)
    with pytest.raises(AttributeError):
        s._value = b"x"
    assert copy.deepcopy(s) == s and copy.copy(s) == s


def test_secret_equality_is_by_value():
    assert Secret("a") == Secret("a") and Secret("a") != Secret("b")


def test_vault_objects_do_not_repr_keys(vault, key, secret_store):
    ref = vault.put(T1, PLAIN)
    texts = [repr(vault), str(vault), repr(vault._keys), str(vault._keys), repr(secret_store.get(T1, ref))]
    for t in texts:
        assert PLAIN not in t
        assert key.hex() not in t and b64(key) not in t and repr(key) not in t
    with pytest.raises(TypeError):
        pickle.dumps(vault._keys)


def test_errors_never_carry_the_secret_or_key(vault, secret_store, clock):
    ref = vault.put(T1, PLAIN)
    caught = []
    for call in (lambda: vault.get(T2, ref), lambda: vault.get(T1, "missing"),
                 lambda: Vault(KeyRing(new_key()), secret_store, clock).get(T1, ref),
                 lambda: vault.put(T1, PLAIN, ref=ref)):
        with pytest.raises(VaultError) as e:
            call()
        caught.append(e.value)
    for exc in caught:
        text = repr(exc) + str(exc) + repr(exc.__cause__) + repr(exc.__context__)
        assert PLAIN not in text


def test_nothing_logged_contains_the_secret(vault, key, caplog, clock, secret_store):
    caplog.set_level(logging.DEBUG)
    ref = vault.put(T1, PLAIN)
    vault.get(T1, ref)
    vault.replace(T1, ref, PLAIN + "-2")
    Vault(KeyRing(new_key(), [key]), secret_store, clock).rewrap_all()
    vault.delete(T1, ref)
    assert caplog.records
    for rec in caplog.records:
        assert PLAIN not in rec.getMessage() and PLAIN not in str(rec.__dict__)


def test_version_is_bound_into_the_ciphertext(vault, secret_store):
    import dataclasses
    ref = vault.put(T1, PLAIN)
    row = secret_store.get(T1, ref)
    secret_store.rows[(T1, ref)] = dataclasses.replace(row, version=row.version + 1)
    with pytest.raises(SecretUnreadable):
        vault.get(T1, ref)


def test_an_old_row_cannot_be_written_back(vault, secret_store):
    ref = vault.put(T1, PLAIN)
    old = secret_store.get(T1, ref)
    vault.replace(T1, ref, "newer")
    assert secret_store.put(old, replace=True) is False
    assert secret_store.put(old, replace=False) is False
    assert vault.get(T1, ref).reveal() == "newer"
    assert secret_store.get(T1, ref).version == 2


def test_versions_count_up_on_replace_and_rewrap(clock):
    store = InMemorySecretStore()
    old, new = new_key(), new_key()
    ref = Vault(KeyRing(old), store, clock).put(T1, PLAIN)
    v = Vault(KeyRing(new, [old]), store, clock)
    v.replace(T1, ref, "two")
    assert store.get(T1, ref).version == 2
    Vault(KeyRing(old), store, clock)  # no-op
    store.rows[(T1, ref)] = store.rows[(T1, ref)]
    assert v.rewrap_all() == 0  # already under the current key
