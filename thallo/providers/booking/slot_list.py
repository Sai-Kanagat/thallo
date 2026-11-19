"""Slot list (booking, local): offer times from the business's own opening hours. No outside call.

Settings: the opening hours per weekday in the tenant's local time, the slot length and how many
days ahead to offer. It does not know about other appointments; the person checks their diary
before agreeing a time (said in the result's task).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider

NAME = "slot_list"
MAX_SLOTS = 12


class SlotListSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    timezone: str = Field(default="Europe/Rome", pattern=r"^[A-Za-z_]+(/[A-Za-z_]+){0,2}$")
    # weekday (0 = Monday) to [open hour, close hour)
    hours: dict[int, tuple[int, int]] = Field(default_factory=lambda: {d: (9, 18) for d in range(5)})
    slot_minutes: int = Field(default=30, ge=10, le=240)
    days_ahead: int = Field(default=7, ge=1, le=30)
    lead_hours: int = Field(default=2, ge=0, le=72)  # no slot sooner than this


def slots(settings: SlotListSettings, now: datetime, limit: int = MAX_SLOTS) -> tuple[datetime, ...]:
    tz = ZoneInfo(settings.timezone)
    start = now + timedelta(hours=settings.lead_hours)
    local = start.astimezone(tz)
    out: list[datetime] = []
    for day in range(settings.days_ahead + 1):
        d = (local + timedelta(days=day)).date()
        hours = settings.hours.get(d.weekday())
        if not hours or not 0 <= hours[0] < hours[1] <= 24:
            continue
        t = datetime(d.year, d.month, d.day, hours[0], tzinfo=tz)
        close = t.replace(hour=0) + timedelta(hours=hours[1])
        while t + timedelta(minutes=settings.slot_minutes) <= close:
            if t >= start:
                out.append(t.astimezone(now.tzinfo))
                if len(out) >= limit:
                    return tuple(out)
            t += timedelta(minutes=settings.slot_minutes)
    return tuple(out)


class SlotList:
    capability = Capability.BOOKING
    name = NAME
    mode = ConnectionMode.LOCAL

    def __init__(self, ctx: ProviderContext) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        try:
            self.settings = SlotListSettings.model_validate(ctx.connection.settings)
        except ValueError:
            self.settings = SlotListSettings()

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=NAME, mode=self.mode, limits={"reads_diary": False},
                              licence_note="built in", sends_externally=False)

    def health(self) -> c.Health:
        return c.Health(connected=True, checked_at=self.clock.now())

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        return c.Estimate(cost_eur=Decimal(0), seconds=0.0)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_cap(self.estimate(request), request)
        return c.RunResult(output=c.BookingResult(slots=slots(self.settings, self.clock.now())), cost_eur=Decimal(0),
                           task="Check your diary before agreeing one of these times.",
                           provenance=c.Provenance(made_by="provider", provider=NAME, mode=self.mode,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name="mock-slot-list")


def create(ctx: ProviderContext) -> SlotList:
    return SlotList(ctx)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.BOOKING, NAME, ConnectionMode.LOCAL, create, settings_schema=SlotListSettings)
