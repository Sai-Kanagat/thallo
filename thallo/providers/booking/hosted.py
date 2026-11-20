"""Shared code for hosted booking pages (Cal.com, Calendly). No `register` here.

Both only read: the public booking link (built from slugs in settings; hosts are constants) and,
for action "slots", the open times. No lead data is sent to the vendor. A failed slots read still
returns the link, so the seller can always send the page.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError

from thallo.connections.registry import ProviderContext
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider

SLUG = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
WINDOW = timedelta(days=7)
MAX_SLOTS = 12


def parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def iso(t: datetime) -> str:
    return t.astimezone(t.tzinfo).isoformat().replace("+00:00", "Z")


class HostedBooking:
    capability: ClassVar[Capability] = Capability.BOOKING
    name: ClassVar[str]
    mode: ClassVar[ConnectionMode] = ConnectionMode.HOSTED
    settings_model: ClassVar[type[BaseModel]]
    licence_note: ClassVar[str]

    def __init__(self, ctx: ProviderContext, *, transport: Any = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.transport = transport
        try:
            self.settings: Any = self.settings_model.model_validate(ctx.connection.settings)
        except ValidationError:
            self.settings = None

    def link(self) -> str:
        raise NotImplementedError

    def fetch_slots(self, start: datetime, end: datetime) -> tuple[datetime, ...]:
        raise NotImplementedError

    def get(self, url: str, params: dict[str, str], auth: Any) -> Any:
        from thallo.providers.publishing.postiz_base import PostizTransport, RawCall

        transport = self.transport or PostizTransport()
        reply = transport(RawCall("GET", f"{url}?{urlencode(params)}", None, None, auth, 15.0))
        if not 200 <= reply.status < 300:
            raise ValueError(f"HTTP {reply.status // 100}xx")
        return reply.body

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=self.mode,
                              limits={"configured": self.settings is not None, "reads_only": True},
                              licence_note=self.licence_note, sends_externally=False)

    def health(self) -> c.Health:
        ok = self.settings is not None
        return c.Health(connected=ok, checked_at=self.clock.now(),
                        detail=None if ok else f"the settings do not match the {self.name} schema")

    def estimate(self, request: c.Request) -> c.Estimate:
        from thallo.connections.errors import InvalidConnection, ProviderUnavailable

        c.check_request(self, request)
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")
        if self.settings is None:
            raise ProviderUnavailable(f"the {self.name} connection is not configured")
        return c.Estimate(cost_eur=Decimal(0), seconds=1.0)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.BookingRequest)
        found: tuple[datetime, ...] = ()
        task = None
        if request.action == "slots":
            now = self.clock.now()
            try:
                found = tuple(t for t in self.fetch_slots(now + timedelta(minutes=5), now + WINDOW) if t > now)
                found = tuple(sorted(found))[:MAX_SLOTS]
            except Exception:  # noqa: BLE001 - the link still works; no vendor body is kept
                task = "Open times could not be read; send the booking link instead."
        return c.RunResult(output=c.BookingResult(link=self.link(), slots=found), cost_eur=Decimal(0), task=task,
                           provenance=c.Provenance(made_by="provider", provider=self.name, mode=self.mode,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.name}")


__all__ = ["SLUG", "HostedBooking", "iso", "parse_time"]
