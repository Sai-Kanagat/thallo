"""Google Calendar mirror: writes only to ONE secondary calendar made for Thallo.

API, Calendar v3 (developers.google.com/calendar/api/v3/reference, as known on 20 Nov 2026; the
fixtures are hand-written from those pages, not captured from a live server):

    POST https://oauth2.googleapis.com/token        refresh token -> access token
    GET  .../calendar/v3/calendars/{calendarId}     {id, summary}
    POST .../calendar/v3/calendars/{calendarId}/events?sendUpdates=none     409 when the id exists
    PUT  .../calendar/v3/calendars/{calendarId}/events/{eventId}?sendUpdates=none

The guard that keeps the owner's calendar untouched, in code, whatever the settings say:
- `calendar_id` must be a secondary calendar id (`...@group.calendar.google.com`); `primary`
  and plain e-mail addresses (a person's main calendar) fail validation;
- before writing, the calendar's own `summary` must start with "Thallo", so a shared team
  calendar is refused even if its id is pasted;
- event ids are `thallo` + the slot uuid's hex (base32hex-safe), and an event is overwritten
  only if it carries `extendedProperties.private.thallo_slot` equal to that slot, so nothing
  else on the calendar is ever changed. No attendees, `sendUpdates=none`.

Credential in the vault: {"client_id", "client_secret", "refresh_token"} for an OAuth client
with the `calendar.app.created` scope (only calendars the app created) where possible.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict, Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.calendar.http_base import DEFAULT_TIMEOUT_S, CalendarHttpProvider, MirrorFailed
from thallo.providers.publishing.postiz_base import PostizTransport, RawCall, TransportError
from thallo.providers.text.base import Auth, check_header_secret, parse_origin, status_class

NAME = "google_calendar"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
ORIGINS = frozenset({("https", "oauth2.googleapis.com", 443), ("https", "www.googleapis.com", 443)})
MARKER = "thallo_slot"
REQUIRED_PREFIX = "Thallo"


class GoogleCalendarSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    calendar_id: str = Field(pattern=r"^[a-z0-9._-]{8,128}@group\.calendar\.google\.com$")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, le=120)


def event_id(slot_id: str) -> str:
    return "thallo" + slot_id.replace("-", "")


class GoogleCalendar(CalendarHttpProvider):
    name = NAME
    settings_model = GoogleCalendarSettings
    licence_note = "Google Calendar API, one dedicated secondary calendar"

    def __init__(self, ctx: ProviderContext, *, transport: Any = None) -> None:
        super().__init__(ctx, transport=transport or PostizTransport())

    def _call(self, method: str, url: str, body: Any = None, *, form: bool = False, auth: Auth | None = None):
        try:
            ok = parse_origin(url) in ORIGINS
        except ValueError:
            ok = False
        if not ok:
            raise MirrorFailed("refused a call to an endpoint this provider does not use")
        data, ctype = None, None
        if body is not None:
            data = urlencode(body).encode() if form else json.dumps(body).encode()
            ctype = "application/x-www-form-urlencoded" if form else "application/json"
        try:
            return self.transport(RawCall(method, url, data, ctype, auth, self.settings.timeout_s))
        except TransportError as exc:
            raise MirrorFailed(f"Google Calendar: {exc}") from None

    def _bearer(self) -> Auth:
        try:
            cred = json.loads(self._secret().reveal())
        except ValueError:
            raise MirrorFailed("the stored Google credential is not JSON; store it again") from None
        keys = ("client_id", "client_secret", "refresh_token")
        if not isinstance(cred, dict) or not all(isinstance(cred.get(k), str) and cred[k] for k in keys):
            raise MirrorFailed("the stored Google credential lacks client_id, client_secret or refresh_token")
        reply = self._call("POST", TOKEN_URL, {"grant_type": "refresh_token", **{k: cred[k] for k in keys}}, form=True)
        token = reply.body.get("access_token") if isinstance(reply.body, dict) and reply.status == 200 else None
        if not isinstance(token, str) or not token:
            raise MirrorFailed(f"the Google token exchange failed (HTTP {status_class(reply.status)})")
        secret = Secret(token)
        try:
            check_header_secret(secret)
        except Exception:
            raise MirrorFailed("the Google access token cannot be sent as a header") from None
        return Auth("authorization", secret, "Bearer ")

    def _mirror(self, slots: list[dict[str, str]]) -> int:
        auth = self._bearer()
        cal = f"{API}/calendars/{quote(self.settings.calendar_id, safe='')}"
        info = self._call("GET", cal, auth=auth)
        summary = info.body.get("summary") if isinstance(info.body, dict) and info.status == 200 else None
        if not isinstance(summary, str) or not summary.startswith(REQUIRED_PREFIX):
            raise MirrorFailed(f"the calendar is not a dedicated Thallo calendar (its name must start with "
                               f"'{REQUIRED_PREFIX}'); nothing was written")
        done = 0
        for s in slots:
            eid = event_id(s["slot_id"])
            event = {"id": eid, "summary": s["title"], "description": s["details"],
                     "start": {"dateTime": s["starts_at"]}, "end": {"dateTime": s["ends_at"]},
                     "transparency": "transparent", "extendedProperties": {"private": {MARKER: s["slot_id"]}}}
            r = self._call("POST", f"{cal}/events?sendUpdates=none", event, auth=auth)
            if r.status == 409:
                got = self._call("GET", f"{cal}/events/{eid}", auth=auth)
                mark = (got.body or {}).get("extendedProperties", {}).get("private", {}).get(MARKER) \
                    if isinstance(got.body, dict) else None
                if mark != s["slot_id"]:
                    raise MirrorFailed("an event with Thallo's id exists without Thallo's marker; left untouched")
                r = self._call("PUT", f"{cal}/events/{eid}?sendUpdates=none", event, auth=auth)
            if not 200 <= r.status < 300:
                raise MirrorFailed(f"Google Calendar refused an event (HTTP {status_class(r.status)}); "
                                   f"{done} written before it")
            done += 1
        return done


def create(ctx: ProviderContext, transport: Any = None) -> GoogleCalendar:
    return GoogleCalendar(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.CALENDAR, NAME, ConnectionMode.HOSTED, create, settings_schema=GoogleCalendarSettings)
