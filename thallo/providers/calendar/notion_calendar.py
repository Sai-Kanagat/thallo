"""Notion mirror: upserts one page per slot into ONE database shared with the integration.

API (developers.notion.com/reference, version 2022-06-28, as known on 20 Nov 2026; fixtures are
hand-written from those pages, not captured from a live server):

    POST https://api.notion.com/v1/databases/{id}/query   filter on the "Thallo slot" text
    POST https://api.notion.com/v1/pages                  {parent: {database_id}, properties}
    PATCH https://api.notion.com/v1/pages/{page_id}       {properties}
    headers  Authorization: Bearer <integration secret>, Notion-Version: 2022-06-28

The integration only sees pages and databases the owner shares with it, so the owner shares one
database made for the plan. Pages are found by the "Thallo slot" property only, so rows the
owner adds by hand are never touched. Properties the database needs: Name (title), When (date),
Thallo slot (rich text), Details (rich text). Nothing is read back.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.calendar.http_base import DEFAULT_TIMEOUT_S, CalendarHttpProvider, MirrorFailed
from thallo.providers.text.base import (
    AddressPolicy,
    Auth,
    HttpCall,
    HttpTransport,
    TextProviderError,
    check_header_secret,
    parse_origin,
    status_class,
)

NAME = "notion_calendar"
API = "https://api.notion.com/v1"
ORIGIN = ("https", "api.notion.com", 443)
VERSION = "2022-06-28"
SLOT_PROP = "Thallo slot"


class NotionCalendarSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    database_id: str = Field(pattern=r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, le=120)


def _text(value: str) -> list[dict]:
    return [{"type": "text", "text": {"content": value[:2000]}}]


class NotionCalendar(CalendarHttpProvider):
    name = NAME
    settings_model = NotionCalendarSettings
    licence_note = "Notion API, one database shared with the integration"

    def __init__(self, ctx: ProviderContext, *, transport: Any = None) -> None:
        super().__init__(ctx, transport=transport or HttpTransport(AddressPolicy.PUBLIC))

    def _call(self, method: str, url: str, body: dict, auth: Auth):
        if parse_origin(url) != ORIGIN:
            raise MirrorFailed("refused a call to an endpoint this provider does not use")
        try:
            r = self.transport(HttpCall(method, url, {"notion-version": VERSION}, body, auth, self.settings.timeout_s))
        except TextProviderError as exc:
            raise MirrorFailed(f"Notion: {exc}") from None
        if not 200 <= r.status < 300:
            raise MirrorFailed(f"Notion answered HTTP {status_class(r.status)}")
        return r.body if isinstance(r.body, dict) else {}

    def _mirror(self, slots: list[dict[str, str]]) -> int:
        secret = self._secret()
        try:
            check_header_secret(secret)
        except Exception:
            raise MirrorFailed("the stored Notion secret cannot be sent as a header; store it again") from None
        auth = Auth("authorization", secret, "Bearer ")
        db = self.settings.database_id
        done = 0
        for s in slots:
            props = {"Name": {"title": _text(s["title"])},
                     "When": {"date": {"start": s["starts_at"], "end": s["ends_at"]}},
                     SLOT_PROP: {"rich_text": _text(s["slot_id"])},
                     "Details": {"rich_text": _text(s["details"])}}
            found = self._call("POST", f"{API}/databases/{db}/query",
                               {"filter": {"property": SLOT_PROP, "rich_text": {"equals": s["slot_id"]}},
                                "page_size": 2}, auth)
            hits = [p for p in found.get("results", []) if isinstance(p, dict) and isinstance(p.get("id"), str)]
            if len(hits) > 1:
                raise MirrorFailed("two Notion pages carry the same Thallo slot; fix the database by hand")
            if hits:
                self._call("PATCH", f"{API}/pages/{hits[0]['id']}", {"properties": props}, auth)
            else:
                self._call("POST", f"{API}/pages", {"parent": {"database_id": db}, "properties": props}, auth)
            done += 1
        return done


def create(ctx: ProviderContext, transport: Any = None) -> NotionCalendar:
    return NotionCalendar(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.CALENDAR, NAME, ConnectionMode.HOSTED, create, settings_schema=NotionCalendarSettings)
