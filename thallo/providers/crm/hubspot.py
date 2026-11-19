"""HubSpot (CRM, one-way push of contacts). Only with the operator's `THALLO_CRM_LIVE_SYNC=1` in
the environment (never a setting) and a private-app token in the vault. Otherwise `estimate()`
raises `ProviderUnavailable` and the registry falls through to the manual CSV connection.

API (HubSpot CRM v3, developers.hubspot.com/docs/api/crm/contacts; fixtures hand-written, not
captured from a live portal):
    POST  https://api.hubapi.com/crm/v3/objects/contacts            {"properties": {...}}  -> {"id": "..."}
    PATCH https://api.hubapi.com/crm/v3/objects/contacts/{id}       {"properties": {...}}  -> {"id": "..."}
    Authorization: Bearer <private app token>

Only the allowlisted fields the sync projects are mapped: firstname, email, phone, and the stage
as `hs_lead_status`. An erase record blanks the contact fields. The records stay out of logs,
provenance and errors. A create whose answer is lost may duplicate on the next run; that is said
in the error so a person checks HubSpot.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from thallo.connections.errors import ConnectionsError, InvalidConnection, ProviderUnavailable
from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider
from thallo.providers.text.base import Auth

NAME = "hubspot"
BASE = "https://api.hubapi.com/crm/v3/objects/contacts"
LIVE_ENV = "THALLO_CRM_LIVE_SYNC"
LEAD_STATUS = {"new": "NEW", "nurturing": "IN_PROGRESS", "contacted": "CONNECTED", "booked": "OPEN_DEAL",
               "held": "OPEN_DEAL", "won": "OPEN_DEAL", "lost": "UNQUALIFIED", "parked": "UNQUALIFIED"}
_ID = re.compile(r"^[0-9]{1,20}$")


class HubSpotSettings(BaseModel):
    """Nothing tenant-writable: the host is a constant and the token lives in the vault."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SyncFailed(ConnectionsError):
    pass


def properties(rec: Mapping[str, Any]) -> dict[str, str]:
    if rec.get("erase"):
        return {"firstname": "", "email": "", "phone": ""}
    props = {"firstname": str(rec.get("name") or "").split(" ")[0][:80], "email": str(rec.get("email") or ""),
             "phone": str(rec.get("phone") or "")}
    if status := LEAD_STATUS.get(str(rec.get("stage") or "")):
        props["hs_lead_status"] = status
    if rec.get("stage") == "won":
        props["lifecyclestage"] = "customer"
    return props


class HubSpot:
    capability = Capability.CRM
    name = NAME
    mode = ConnectionMode.HOSTED

    def __init__(self, ctx: ProviderContext, *, transport: Any = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.transport = transport
        self.environ = environ

    def _live(self) -> bool:
        import os

        return (os.environ if self.environ is None else self.environ).get(LIVE_ENV) == "1"

    def _problem(self) -> str | None:
        if self.ctx.connection.secret_ref is None or self.ctx.vault is None:
            return "no token is stored for this connection"
        return None

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=NAME, mode=self.mode,
                              limits={"configured": self._problem() is None, "live_sync": self._live(),
                                      "one_way": True},
                              licence_note="HubSpot terms; the client's own portal", sends_externally=True)

    def health(self) -> c.Health:
        now = self.clock.now()
        if (p := self._problem()) is not None:
            return c.Health(connected=False, checked_at=now, detail=p)
        if not self._live():
            return c.Health(connected=False, checked_at=now, detail="live sync is off: export CSV instead")
        return c.Health(connected=True, checked_at=now)

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")
        if (p := self._problem()) is not None:
            raise ProviderUnavailable(f"HubSpot is not configured: {p}")
        if not self._live():
            raise ProviderUnavailable("live CRM sync is off; the manual connection exports CSV")
        return c.Estimate(cost_eur=Decimal(0), seconds=1.0)

    def _call(self, method: str, url: str, props: dict[str, str]) -> str:
        from thallo.providers.publishing.postiz_base import PostizTransport, RawCall, TransportError

        secret = self.ctx.secret()
        if secret is None or not secret:
            raise SyncFailed("the vault returned no token for this connection")
        transport = self.transport or PostizTransport()
        body = json.dumps({"properties": props}).encode()
        try:
            reply = transport(RawCall(method, url, body, "application/json", Auth("authorization", secret, "Bearer ")))
        except TransportError as exc:
            raise SyncFailed(f"HubSpot: {'may have been received' if exc.reached else 'not sent'}") from None
        if not 200 <= reply.status < 300:
            raise SyncFailed(f"HubSpot answered HTTP {reply.status // 100}xx")
        cid = reply.body.get("id") if isinstance(reply.body, dict) else None
        if not isinstance(cid, str) or not _ID.match(cid):
            raise SyncFailed("HubSpot returned no contact id; check the portal before syncing again")
        return cid

    def run(self, request: c.Request) -> c.RunResult:
        est = self.estimate(request)
        c.check_cap(est, request)
        assert isinstance(request, c.CrmSyncRequest)
        ids: dict[str, str] = {}
        for rec in request.records:
            pid = str(rec.get("thallo_id") or "")
            if not pid:
                continue
            ext = rec.get("external_id")
            try:
                if isinstance(ext, str) and _ID.match(ext):
                    ids[pid] = self._call("PATCH", f"{BASE}/{ext}", properties(rec))
                elif rec.get("erase"):
                    continue  # never created there: nothing to erase
                else:
                    ids[pid] = self._call("POST", BASE, properties(rec))
            except SyncFailed:
                if not ids:
                    raise
                break  # the rest go next time; what was confirmed is recorded
        return c.RunResult(output=c.CrmSyncResult(synced=len(ids), external_ids=ids), cost_eur=est.cost_eur,
                           provenance=c.Provenance(made_by="provider", provider=NAME, mode=self.mode,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name="mock-hubspot")


def create(ctx: ProviderContext, transport: Any = None, environ: Mapping[str, str] | None = None) -> HubSpot:
    return HubSpot(ctx, transport=transport, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.CRM, NAME, ConnectionMode.HOSTED, create, settings_schema=HubSpotSettings)
