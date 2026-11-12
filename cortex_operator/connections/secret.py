"""A string that cannot leak by accident.

`Secret` wraps a value so that repr, str, format, f-strings, pydantic dumps, JSON, pickling and
logging all show a fixed placeholder. The only way out is `reveal()`, which a provider calls at
the moment it builds a request. Never log, store or return what `reveal()` gives back.
"""
from __future__ import annotations

import hmac
import math
import re
from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

REDACTED = "**redacted**"


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str | bytes | Secret) -> None:
        if isinstance(value, Secret):
            value = value.reveal()
        if isinstance(value, str):
            value = value.encode()
        if not isinstance(value, bytes):
            raise TypeError("a Secret wraps str or bytes")
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("a Secret is immutable")

    def reveal(self) -> str:
        return self._value.decode()

    def reveal_bytes(self) -> bytes:
        return self._value

    def size(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return hmac.compare_digest(self._value, other._value)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> Any:
        raise TypeError("a Secret cannot be pickled")

    def __copy__(self) -> Secret:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Secret:
        return self

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        def validate(value: Any) -> Secret:
            if isinstance(value, Secret):
                return value
            if isinstance(value, str | bytes):
                return Secret(value)
            raise ValueError("expected a secret string")

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda _v: REDACTED, return_schema=core_schema.str_schema()),
        )


_SECRET_TOKENS = frozenset({
    "secret", "secrets", "token", "password", "passwd", "pwd", "key", "apikey", "accesskey",
    "privatekey", "signingkey", "credential", "credentials", "authorization", "auth", "bearer", "dsn",
    "webhook", "webhooks", "signature", "cookie", "passphrase",
})
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+\S{8,}", re.I),
    re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}"),
    re.compile(r"\bsk[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"hooks\.slack\.com/(?:services|triggers)/", re.I),
    re.compile(r"hooks\.zapier\.com/hooks/", re.I),
    re.compile(r"(?:discord(?:app)?\.com/api/webhooks/|webhook\.office\.com/|maker\.ifttt\.com/trigger/)", re.I),
    re.compile(r"\b(?:ghp|gho|ghs|xox[abprs]|AIza)[-_A-Za-z0-9]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


def _entropy(text: str) -> float:
    counts = {ch: text.count(ch) for ch in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def looks_secret_key(name: str) -> bool:
    spaced = _CAMEL.sub("_", name)
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]
    if not tokens or tokens[-1] in ("ref", "hint"):
        return False  # secret_ref and friends point at a secret, they are not one
    return any(t in _SECRET_TOKENS for t in tokens) or "".join(tokens) in _SECRET_TOKENS


def looks_secret_value(value: str) -> bool:
    if any(p.search(value) for p in _VALUE_PATTERNS):
        return True
    return any(_entropy(m.group()) >= 3.5 and not m.group().isalpha() for m in _LONG_TOKEN.finditer(value))


def find_secret_keys(settings: Any, _path: str = "") -> list[str]:
    """Paths in a settings structure that look like a raw secret, by key name or by value.
    Rows hold a `secret_ref` instead; the vault holds the secret."""
    found: list[str] = []
    if isinstance(settings, dict):
        for k, v in settings.items():
            p = f"{_path}.{k}" if _path else str(k)
            if isinstance(k, str) and looks_secret_key(k):
                found.append(p)
            else:
                found.extend(find_secret_keys(v, p))
    elif isinstance(settings, list | tuple):
        for i, v in enumerate(settings):
            found.extend(find_secret_keys(v, f"{_path}[{i}]"))
    elif isinstance(settings, str) and looks_secret_value(settings):
        found.append(_path or "<value>")
    return found
