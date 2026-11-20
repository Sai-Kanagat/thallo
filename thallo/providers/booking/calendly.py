"""Calendly (booking, hosted, read only). The public page is https://calendly.com/{user}/{event_slug}.

Open times, from developer.calendly.com/openapi/calendly-api.yaml (read 19 Nov 2026; fixtures
hand-written): GET https://api.calendly.com/event_type_available_times
    ?event_type=https://api.calendly.com/event_types/{uuid}&start_time=..&end_time=..   (31 days at most)
    Authorization: Bearer <personal access token>, scope event_types:read
    {"collection": [{"status": "available", "start_time": "2026-11-20T09:00:00Z", "scheduling_url": "..."}]}
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.booking.hosted import SLUG, HostedBooking, iso, parse_time
from thallo.providers.text.base import Auth

NAME = "calendly"
TIMES_URL = "https://api.calendly.com/event_type_available_times"
EVENT_TYPE = "https://api.calendly.com/event_types/"


class CalendlySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_slug: str = Field(pattern=SLUG)
    event_slug: str = Field(pattern=SLUG)
    event_type_uuid: str = Field(pattern=r"^[A-Za-z0-9-]{8,64}$")


def parse_times(body: Any) -> tuple[datetime, ...]:
    rows = body.get("collection") if isinstance(body, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else ():
        if isinstance(r, dict) and r.get("status") == "available" and (t := parse_time(r.get("start_time"))):
            out.append(t)
    return tuple(out)


class Calendly(HostedBooking):
    name = NAME
    settings_model = CalendlySettings
    licence_note = "Calendly terms; the client's own account"

    def link(self) -> str:
        return f"https://calendly.com/{self.settings.user_slug}/{self.settings.event_slug}"

    def fetch_slots(self, start: datetime, end: datetime) -> tuple[datetime, ...]:
        secret = self.ctx.secret()
        if secret is None or not secret:
            raise ValueError("no token stored")
        body = self.get(TIMES_URL, {"event_type": EVENT_TYPE + self.settings.event_type_uuid,
                                    "start_time": iso(start), "end_time": iso(end)},
                        Auth("authorization", secret, "Bearer "))
        return parse_times(body)


def create(ctx: ProviderContext, transport: Any = None) -> Calendly:
    return Calendly(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.BOOKING, NAME, ConnectionMode.HOSTED, create, settings_schema=CalendlySettings)
