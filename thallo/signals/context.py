"""What the intake stages for one pull, so a provider called through the registry can reach it.

The contract's `AnalyticsRequest` carries no bytes and `ProviderContext` no database handle, so
the intake opens a `staged(...)` block around each call:

    uploads   ref -> CSV bytes a person uploaded (the CSV provider reads `request.refs`)
    touches   a reader of this tenant's own touch log (the own-logs provider)
    raw       every raw reply a provider kept, by sha256, for `signal_pull`

Outside a block the CSV and own-logs providers are unavailable, so the registry falls through to
the manual pack, and raw replies are dropped (a provider still returns its `raw_ref`).
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# (start, end) -> [(day, touch kind, count)]
TouchReader = Callable[[date, date], list[tuple[date, str, int]]]


@dataclass
class Staged:
    tenant_id: str
    uploads: Mapping[str, bytes] = field(default_factory=dict)
    touches: TouchReader | None = None
    raw: dict[str, Any] = field(default_factory=dict)


_current: ContextVar[Staged | None] = ContextVar("thallo_signals_staged", default=None)


def current() -> Staged | None:
    return _current.get()


@contextmanager
def staged(tenant_id: str, *, uploads: Mapping[str, bytes] | None = None,
           touches: TouchReader | None = None) -> Iterator[Staged]:
    s = Staged(tenant_id, dict(uploads or {}), touches)
    token = _current.set(s)
    try:
        yield s
    finally:
        _current.reset(token)


def keep_raw(sha: str, body: Any) -> None:
    s = _current.get()
    if s is not None:
        s.raw[sha] = body
