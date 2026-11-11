"""Time. Every component takes a Clock. Nothing calls datetime.now() directly.

All times are timezone-aware UTC. A naive datetime is a bug and is refused.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime refused: use timezone-aware UTC")
    return value.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """The real clock. Used only by the worker entry point."""

    def now(self) -> datetime:
        return datetime.now(UTC)  # the one allowed call, see scripts/check_rules.py


class FakeClock:
    """A clock that only moves when told to. Used by every test and the demo."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta | None = None, **kwargs: float) -> datetime:
        step = delta if delta is not None else timedelta(**kwargs)
        if step < timedelta(0):
            raise ValueError("a fake clock never moves backwards")
        self._now = self._now + step
        return self._now

    def set(self, value: datetime) -> datetime:
        value = _require_aware(value)
        if value < self._now:
            raise ValueError("a fake clock never moves backwards")
        self._now = value
        return self._now


# The reference start for fixtures and the demo week: a Monday, 09:00 UTC.
FAKE_EPOCH = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
