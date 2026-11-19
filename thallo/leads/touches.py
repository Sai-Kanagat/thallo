"""What the lead desk needs from tracking (WP-12, built in parallel): a feed of first touches.

WP-12 owns links, reference codes and the touch table's writers. Until it merges, the desk takes
`TouchEvent`s through this small interface, and tests and the demo use `FakeTouchFeed`. When WP-12
lands, its feed implements `TouchFeed` and nothing in the desk changes.

Everything in a TouchEvent that a person typed (name, form answers, self report) is untrusted.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from thallo.contracts.enums import TouchKind

# Which heat action each touch kind counts as (play heat_actions keys).
HEAT_ACTION: dict[TouchKind, str] = {
    TouchKind.LINK: "link",
    TouchKind.WHATSAPP_CODE: "whatsapp_chat",
    TouchKind.FORM: "form",
    TouchKind.CALL: "call",
    TouchKind.WALK_IN_CODE: "walk_in_code",
}


@dataclass(frozen=True)
class TouchEvent:
    kind: TouchKind
    occurred_at: datetime
    display_name: str | None = None
    phone: str | None = None
    email: str | None = None
    code: str | None = None
    variant_id: str | None = None
    ad_id: str | None = None
    tracked_link_id: str | None = None
    self_report: str | None = None  # "how did you hear about us", in their words
    answers: dict[str, Any] = field(default_factory=dict)  # form fields matching question keys
    extra_actions: tuple[str, ...] = ()  # e.g. "pricing_page", "offer_view"


@runtime_checkable
class TouchFeed(Protocol):
    def pending(self, tenant_id: str) -> Iterable[TouchEvent]:
        """Touches not yet taken by the desk. Taking them is acknowledged with `taken`."""
        ...

    def taken(self, tenant_id: str, event: TouchEvent) -> None: ...


class FakeTouchFeed:
    def __init__(self) -> None:
        self._queue: dict[str, list[TouchEvent]] = {}

    def push(self, tenant_id: str, event: TouchEvent) -> None:
        self._queue.setdefault(tenant_id, []).append(event)

    def pending(self, tenant_id: str) -> list[TouchEvent]:
        return list(self._queue.get(tenant_id, []))

    def taken(self, tenant_id: str, event: TouchEvent) -> None:
        self._queue.get(tenant_id, []).remove(event)


__all__ = ["HEAT_ACTION", "FakeTouchFeed", "TouchEvent", "TouchFeed"]
