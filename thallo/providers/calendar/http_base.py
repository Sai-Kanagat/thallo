"""Shared code for the calendar mirror providers. No `register` here.

A calendar provider answers a `CalendarRequest` by upserting one event or page per slot, keyed
by the slot id, into ONE dedicated calendar or database named in settings. It never reads the
owner's other calendars, never invites anyone, never sends notifications and never reads status
back (`comments_in` stays empty: the week plan in Thallo is the record).

A slot that does not have the planner's shape (`slot_id` a uuid, `title`, `starts_at`,
`ends_at`) is skipped and not counted, so a stray dict cannot become an event.

Settings hold only validated ids and a timeout, never a URL or host. Hosts are constants per
provider, every call is checked against them, and an unconfigured provider raises
`ProviderUnavailable` from `estimate()` so the registry falls through to the manual provider.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from thallo.connections.errors import ConnectionsError, InvalidConnection, ProviderUnavailable
from thallo.connections.registry import ProviderContext
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider

DEFAULT_TIMEOUT_S = 30.0
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_TITLE = 200
MAX_DETAILS = 1500


class MirrorFailed(ConnectionsError):
    """The calendar did not take the write. The message is safe to store and show."""


def clean_slots(slots: tuple[dict[str, Any], ...]) -> list[dict[str, str]]:
    out = []
    for s in slots:
        if not isinstance(s, dict):
            continue
        sid, title, start, end = s.get("slot_id"), s.get("title"), s.get("starts_at"), s.get("ends_at")
        if not (isinstance(sid, str) and UUID.match(sid) and isinstance(title, str) and title.strip()):
            continue
        try:
            a, b = datetime.fromisoformat(str(start)), datetime.fromisoformat(str(end))
        except ValueError:
            continue
        if a.tzinfo is None or b.tzinfo is None or b <= a:
            continue
        details = s.get("details") if isinstance(s.get("details"), str) else ""
        out.append({"slot_id": sid, "title": title[:MAX_TITLE], "starts_at": a.isoformat(), "ends_at": b.isoformat(),
                    "details": details[:MAX_DETAILS]})
    return out


class CalendarHttpProvider:
    capability: ClassVar[Capability] = Capability.CALENDAR
    name: ClassVar[str]
    mode: ClassVar[ConnectionMode] = ConnectionMode.HOSTED
    settings_model: ClassVar[type[BaseModel]]
    licence_note: ClassVar[str] = ""

    def __init__(self, ctx: ProviderContext, *, transport: Any = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.transport = transport
        try:
            self.settings: Any = self.settings_model.model_validate(ctx.connection.settings)
        except ValidationError:
            self.settings = None

    def problem(self) -> str | None:
        if self.settings is None:
            return f"the connection settings do not match the {self.name} schema"
        if self.ctx.connection.secret_ref is None or self.ctx.vault is None:
            return "no credential is stored for this connection"
        return None

    def _secret(self):
        secret = self.ctx.secret()
        if secret is None or not secret:
            raise MirrorFailed("the vault returned no credential for this connection")
        return secret

    def _check_tenant(self, request: c.Request) -> None:
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")

    def describe(self) -> c.ProviderInfo:
        # It writes to a calendar outside Thallo, though only to the dedicated one: say so.
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=self.mode,
                              limits={"configured": self.problem() is None, "direction": "one way out"},
                              licence_note=self.licence_note, sends_externally=True)

    def health(self) -> c.Health:
        issue = self.problem()
        return c.Health(connected=issue is None, checked_at=self.clock.now(), detail=issue)

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        self._check_tenant(request)
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"the {self.name} connection is not configured: {issue}")
        return c.Estimate(cost_eur=Decimal(0), seconds=2.0)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)
        self._check_tenant(request)
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.CalendarRequest)
        slots = clean_slots(request.slots)
        mirrored = self._mirror(slots) if slots else 0
        return c.RunResult(output=c.CalendarResult(mirrored=mirrored), cost_eur=Decimal(0),
                           provenance=c.Provenance(made_by="provider", provider=self.name, mode=self.mode,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.name}")

    def _mirror(self, slots: list[dict[str, str]]) -> int:
        raise NotImplementedError


__all__ = ["DEFAULT_TIMEOUT_S", "CalendarHttpProvider", "MirrorFailed", "clean_slots"]
