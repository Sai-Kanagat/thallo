"""Pre-flight checks, idempotent delivery and retries, plus a mock channel."""
from __future__ import annotations

from dataclasses import replace

from thallo.contracts import Item
from thallo.truth import render

LIMITS = {"linkedin": 3000, "instagram": 2200, "tiktok": 2200, "x": 280}


def preflight(item: Item) -> list[str]:
    errors = []
    text = render(item.text)
    if not text.strip():
        errors.append("empty text")
    limit = LIMITS.get(item.channel)
    if limit and len(text) > limit:
        errors.append(f"over {limit} characters")
    if item.channel in ("instagram", "tiktok") and not item.media:
        errors.append("media required")
    return errors


class MockPublisher:
    """Stands in for the scheduler. Sends nothing anywhere."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sent: dict[str, str] = {}
        self.texts: list[str] = []
        self.calls = 0

    def publish(self, key: str, item: Item) -> str:
        self.calls += 1
        if key in self.sent:
            return self.sent[key]
        if self.failures:
            self.failures -= 1
            raise ConnectionError("mock failure")
        self.texts.append(item.text)
        self.sent[key] = f"https://mock.invalid/{item.channel}/{item.id}"
        return self.sent[key]


def deliver(publisher, key: str, item: Item, attempts: int = 3) -> str | None:
    public = replace(item, text=render(item.text))
    for _ in range(attempts):
        try:
            return publisher.publish(key, public)
        except ConnectionError:
            continue
        except (TimeoutError, ValueError):
            # A timeout may have posted, so a retry could double post.
            return None
    return None
