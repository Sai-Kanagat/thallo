"""The vault refuses a secret it would have to change to use (empty, control characters, edge
spaces) and never echoes it. Refused, not stripped: a silently edited secret is worse."""
from __future__ import annotations

import logging
import traceback

import pytest

from thallo.connections import Secret, VaultError

from .conftest import T1

BASE = "fake-key-for-test"
BAD = {
    "empty": "",
    "trailing newline": BASE + "\n",
    "crlf injection": BASE + "\r\nx-injected: yes",
    "nul": BASE + "\x00tail",
    "tab": BASE + "\tx",
    "del": BASE + "\x7f",
    "leading space": " " + BASE,
    "trailing space": BASE + " ",
}


def catch(fn, *args) -> VaultError:
    """Returns what fn raised, minus this harness frame (its args are the test's own input)."""
    try:
        fn(*args)
    except VaultError as exc:
        return exc.with_traceback(exc.__traceback__.tb_next)  # drop this harness frame
    raise AssertionError("expected VaultError")


def assert_quiet(exc: VaultError, logs: str) -> None:
    te = traceback.TracebackException.from_exception(exc, capture_locals=True)
    text = (str(exc) + repr(exc) + repr(exc.args) + "".join(traceback.format_exception(exc))
            + "".join(te.format()) + repr(exc.__cause__) + repr(exc.__context__) + logs)
    assert BASE not in text


@pytest.mark.parametrize("kind", list(BAD))
@pytest.mark.parametrize("wrap", [str, Secret])
def test_put_refuses_without_echo(vault, secret_store, kind, wrap, caplog):
    caplog.set_level(logging.DEBUG)
    exc = catch(vault.put, T1, wrap(BAD[kind]))
    assert_quiet(exc, caplog.text)
    assert secret_store.list_for_tenant(T1) == []  # nothing was stored


@pytest.mark.parametrize("kind", list(BAD))
def test_replace_refuses_without_echo(vault, kind, caplog):
    ref = vault.put(T1, BASE)
    caplog.set_level(logging.DEBUG)
    exc = catch(vault.replace, T1, ref, Secret(BAD[kind]))
    assert_quiet(exc, caplog.text)
    assert vault.get(T1, ref).reveal() == BASE  # the stored value is unchanged


def test_inner_spaces_and_non_ascii_are_kept(vault):
    for value in (BASE + " two", BASE + "é"):  # the vault stores them; the transport decides
        assert vault.get(T1, vault.put(T1, value)).reveal() == value
