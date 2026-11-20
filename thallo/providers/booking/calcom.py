"""Cal.com (booking, hosted, read only). The public booking page is https://cal.com/{username}/{event_slug}.

Slots, from cal.com/docs/api-reference/v2/slots (read 19 Nov 2026; fixtures hand-written):
    GET https://api.cal.com/v2/slots?eventTypeId=..&start=..&end=..&timeZone=..
    cal-api-version: 2024-09-04          (required; other values fall back to an older version)
    {"status": "success", "data": {"2033-09-05": [{"start": "2033-09-05T09:00:00.000+02:00"}, ...]}}
No key is sent: this assumes the slots of a public event type are readable without one (not
verified against a live account; the docs page did not say). The shared transport carries
one extra header: the fixed API version goes in that slot (it is not a secret).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.booking.hosted import SLUG, HostedBooking, iso, parse_time
from thallo.providers.text.base import Auth

NAME = "calcom"
SLOTS_URL = "https://api.cal.com/v2/slots"
API_VERSION = "2024-09-04"


class CalcomSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    username: str = Field(pattern=SLUG)
    event_slug: str = Field(pattern=SLUG)
    event_type_id: int = Field(ge=1, le=10**12)
    time_zone: str = Field(default="Europe/Rome", pattern=r"^[A-Za-z_]+(/[A-Za-z_]+){0,2}$")


def parse_slots(body: Any) -> tuple[datetime, ...]:
    data = body.get("data") if isinstance(body, dict) else None
    out: list[datetime] = []
    if isinstance(data, dict):
        for day in data.values():
            for s in day if isinstance(day, list) else ():
                t = parse_time(s.get("start") if isinstance(s, dict) else s)
                if t is not None:
                    out.append(t)
    return tuple(out)


class Calcom(HostedBooking):
    name = NAME
    settings_model = CalcomSettings
    licence_note = "Cal.com terms; the client's own account"

    def link(self) -> str:
        return f"https://cal.com/{self.settings.username}/{self.settings.event_slug}"

    def fetch_slots(self, start: datetime, end: datetime) -> tuple[datetime, ...]:
        body = self.get(SLOTS_URL, {"eventTypeId": str(self.settings.event_type_id), "start": iso(start),
                                    "end": iso(end), "timeZone": self.settings.time_zone},
                        Auth("cal-api-version", Secret(API_VERSION)))
        return parse_slots(body)


def create(ctx: ProviderContext, transport: Any = None) -> Calcom:
    return Calcom(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.BOOKING, NAME, ConnectionMode.HOSTED, create, settings_schema=CalcomSettings)
