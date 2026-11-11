"""A string that cannot leak by accident.

`Secret` wraps a value so that repr, str, format, f-strings, pydantic dumps, JSON, pickling and
logging all show a fixed placeholder. The only way out is `reveal()`, which a provider calls at
the moment it builds a request. Never log, store or return what `reveal()` gives back.
"""
from __future__ import annotations

import hmac
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


_SECRETISH = ("secret", "token", "password", "passwd", "api_key", "apikey", "private_key", "authorization",
              "bearer", "credential")


def looks_secret_key(name: str) -> bool:
    low = name.lower().replace("-", "_")
    return any(s in low for s in _SECRETISH)


def find_secret_keys(settings: Any, _path: str = "") -> list[str]:
    """Paths in a settings dict whose key name suggests a raw secret. Rows hold a secret_ref instead."""
    found: list[str] = []
    if isinstance(settings, dict):
        for k, v in settings.items():
            p = f"{_path}.{k}" if _path else str(k)
            if isinstance(k, str) and looks_secret_key(k) and k != "secret_ref":
                found.append(p)
            found.extend(find_secret_keys(v, p))
    elif isinstance(settings, list | tuple):
        for i, v in enumerate(settings):
            found.extend(find_secret_keys(v, f"{_path}[{i}]"))
    return found
