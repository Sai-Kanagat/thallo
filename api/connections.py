"""The connections screen: list, add, enable or disable, reorder, replace a key, remove, test.

Rules from the security reviews:
- A key goes only to the vault, through the registry (`ConnectionRegistry.add`, which calls
  `Vault.put`) or `Vault.replace` for an existing ref. No route ever returns it, logs it or puts it
  in an error: the response says only whether a key is stored.
- Settings go through `registry.add`, which validates them against the provider's schema. Before
  that, this API accepts only the keys in `SETTABLE` for that provider, each a plain scalar, so no
  setting can name a binary, a filesystem path or a host. A provider whose required settings
  include a host that decides where the key goes (`openai_compatible.base_url`) is not offered
  here; the operator configures it. Where a provider's origin is fixed by the operator or by code,
  it is shown read only.
- Listing shows only those same keys, so nothing else stored in settings is rendered.
- No file upload of any kind lives here.

  GET    /api/connections
  GET    /api/connections/providers
  POST   /api/connections                 {capability, provider, model?, role?, settings?, secret?}
  POST   /api/connections/{id}/enabled    {enabled}
  POST   /api/connections/order           {ids}
  POST   /api/connections/{id}/secret     {secret}
  DELETE /api/connections/{id}
  POST   /api/connections/{id}/test       runs the provider's health() once
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from api.context import CONNECTION_EDITORS, Ctx, member, services
from api.errors import ApiError, bad_request, conflict, not_found
from thallo.connections.errors import ConnectionsError, UnknownProvider
from thallo.connections.registry import MANUAL, ConnectionRegistry
from thallo.connections.secret import Secret
from thallo.contracts.enums import Capability, ConnectionMode, TextRole
from thallo.contracts.ids import is_id
from thallo.contracts.models import Connection, CostEntry

log = logging.getLogger("thallo.api.connections")
router = APIRouter(prefix="/api")

_PRICES = ("input_usd_per_mtok", "output_usd_per_mtok", "timeout_s")
# The only settings this API takes or shows, per (capability, provider). Every value a scalar.
SETTABLE: Mapping[tuple[Capability, str], tuple[str, ...]] = {
    (Capability.TEXT, "claude"): (*_PRICES, "effort"),
    (Capability.TEXT, "gemini"): _PRICES,
    (Capability.TEXT, "local"): _PRICES,
    (Capability.IMAGE, "template_render"): ("language",),
}
# Providers whose required settings name a host that decides where the key goes.
NOT_OFFERED = frozenset({(Capability.TEXT, "openai_compatible")})
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9._ -]{0,64}$")
_PATHISH = re.compile(r"[/\\~]|://|^\.|\.\.|^[A-Za-z]:")


def fixed_origin(capability: Capability, provider: str, environ: Mapping[str, str]) -> str | None:
    """Where a provider's key or data goes, as fixed by code or the operator. Read only."""
    if capability is Capability.TEXT and provider == "claude":
        from thallo.providers.text.claude import BASE_URL
        return BASE_URL
    if capability is Capability.TEXT and provider == "gemini":
        from thallo.providers.text.gemini import BASE_URL
        return BASE_URL
    if capability is Capability.TEXT and provider == "local":
        from thallo.providers.text.local import DEFAULT_BASE_URL, URL_ENV
        return environ.get(URL_ENV) or DEFAULT_BASE_URL
    if capability is Capability.IMAGE and provider == "template_render":
        return "the operator's brand folder for this tenant"
    return None


class AddBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: Capability
    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    role: TextRole | None = None
    settings: dict[str, Any] = Field(default_factory=dict, max_length=16)
    secret: SecretStr | None = Field(default=None, max_length=64 * 1024)


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=50)


class SecretBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: SecretStr = Field(max_length=64 * 1024)


def _registry(request: Request, ctx: Ctx) -> ConnectionRegistry:
    svc = services(request)
    conns, _secrets, health = svc.backend.connection_stores()
    return ConnectionRegistry(clock=svc.clock, providers=svc.providers, connections=conns, vault=svc.vault,
                              health=health, mock_tenants=svc.mock_tenants, environ=svc.environ)


def _scrub(message: str, *secrets: SecretStr | None) -> str:
    """Errors from the registry name fields, never values; this makes sure of it."""
    for s in secrets:
        if s is not None and s.get_secret_value() and s.get_secret_value() in message:
            return "the connection was refused"
    return message


def _hostile(key: str, value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float | Decimal):
        return None
    if not isinstance(value, str):
        return f"{key} must be a plain value"
    if _PATHISH.search(value) or not _SAFE_TEXT.match(value):
        return f"{key} may not name a path, a host or a program"
    return None


def _check_settings(capability: Capability, provider: str, settings: dict[str, Any]) -> dict[str, Any]:
    allowed = SETTABLE.get((capability, provider), ())
    refused = sorted(k for k in settings if k not in allowed)
    if refused:
        raise bad_request(f"these settings cannot be set here: {refused}")
    problems = [p for k, v in settings.items() if (p := _hostile(k, v))]
    if problems:
        raise bad_request("; ".join(problems))
    return settings


def _offered(request: Request, ctx: Ctx, registry: ConnectionRegistry) -> list[dict]:
    svc = services(request)
    out = []
    for cap in Capability:
        for name in svc.providers.names(cap):
            entry = svc.providers.get(cap, name)
            hidden_mock = entry.mode is ConnectionMode.MOCK and not registry.mock_allowed(ctx.tenant_id)
            if (cap, name) in NOT_OFFERED or hidden_mock:
                continue
            out.append({"capability": cap.value, "provider": name, "mode": entry.mode.value,
                        "settings": list(SETTABLE.get((cap, name), ())),
                        "takes_secret": entry.mode is ConnectionMode.HOSTED,
                        "fixed_origin": fixed_origin(cap, name, svc.environ)})
    return out


def _cost_this_month(request: Request, ctx: Ctx) -> dict[tuple[str, str, str | None], Decimal]:
    svc = services(request)
    now: datetime = svc.clock.now()
    totals: dict[tuple[str, str, str | None], Decimal] = {}
    with svc.backend.tenant(ctx.tenant_id) as tx:
        for e in tx.list_all(CostEntry):
            if e.occurred_at.year == now.year and e.occurred_at.month == now.month:
                key = (e.capability.value, e.provider, e.model)
                totals[key] = totals.get(key, Decimal(0)) + Decimal(e.cost_eur)
    return totals


def _view(row: Connection, registry: ConnectionRegistry, costs: Mapping, environ: Mapping[str, str]) -> dict:
    shown = SETTABLE.get((row.capability, row.provider), ())
    health = registry.health.last_connected(row.tenant_id, row.id) if registry.health else None
    return {
        "id": row.id, "capability": row.capability.value, "provider": row.provider, "mode": row.mode.value,
        "model": row.model, "role": row.role.value if row.role else None, "scope": row.scope,
        "priority": row.priority, "enabled": row.enabled,
        "settings": {k: row.settings[k] for k in shown if k in row.settings},
        "has_secret": row.secret_ref is not None,
        "health": None if health is None else ("connected" if health else "down"),
        "cost_this_month_eur": str(costs.get((row.capability.value, row.provider, row.model), Decimal(0))),
        "quota": None,  # no provider reports quota yet
        "fixed_origin": fixed_origin(row.capability, row.provider, environ),
        "is_floor": row.provider == MANUAL and row.scope == "tenant",
    }


@router.get("/connections")
def list_connections(request: Request) -> dict:
    ctx = member(request, CONNECTION_EDITORS)
    registry = _registry(request, ctx)
    registry.ensure_manual(ctx.tenant_id)
    costs = _cost_this_month(request, ctx)
    rows = sorted(registry.connections.list_for_tenant(ctx.tenant_id),
                  key=lambda r: (r.capability.value, r.scope, r.scope_ref or "", r.role or "", r.priority))
    env = services(request).environ
    by_cap: dict[str, list[dict]] = {c.value: [] for c in Capability}
    for r in rows:
        by_cap[r.capability.value].append(_view(r, registry, costs, env))
    return {"capabilities": [{"capability": k, "connections": v} for k, v in by_cap.items()]}


@router.get("/connections/providers")
def providers(request: Request) -> dict:
    ctx = member(request, CONNECTION_EDITORS)
    return {"providers": _offered(request, ctx, _registry(request, ctx))}


def _get(registry: ConnectionRegistry, ctx: Ctx, connection_id: str) -> Connection:
    if not is_id(connection_id):
        raise not_found()
    row = registry.connections.get(ctx.tenant_id, connection_id)
    if row is None or row.tenant_id != ctx.tenant_id:
        raise not_found()
    return row


@router.post("/connections", status_code=201)
def add(body: AddBody, request: Request) -> dict:
    ctx = member(request, CONNECTION_EDITORS, unsafe=True)
    registry = _registry(request, ctx)
    if (body.capability, body.provider) in NOT_OFFERED:
        raise bad_request("this provider is configured by the operator, not here")
    if body.model is not None and not _MODEL.match(body.model):
        raise bad_request("model must be a model id (letters, digits, dot, dash, colon, underscore)")
    settings = _check_settings(body.capability, body.provider, dict(body.settings))
    try:
        entry = registry.providers.get(body.capability, body.provider)
    except UnknownProvider:
        raise bad_request("no such provider") from None
    if body.secret is not None and entry.mode is not ConnectionMode.HOSTED:
        raise bad_request("this provider takes no key")
    try:
        row = registry.add(ctx.tenant_id, body.capability, body.provider, model=body.model, role=body.role,
                           settings=settings,
                           secret=Secret(body.secret.get_secret_value()) if body.secret is not None else None)
    except ConnectionsError as exc:
        raise bad_request(_scrub(str(exc), body.secret)) from None
    except ValueError:
        raise bad_request("the key was refused; paste it again without spaces or line breaks") from None
    log.info("connection added tenant=%s capability=%s provider=%s", ctx.tenant_id, row.capability.value, row.provider)
    return {"connection": _view(row, registry, {}, services(request).environ)}


@router.post("/connections/{connection_id}/enabled")
def set_enabled(connection_id: str, body: EnabledBody, request: Request) -> dict:
    ctx = member(request, CONNECTION_EDITORS, unsafe=True)
    registry = _registry(request, ctx)
    _get(registry, ctx, connection_id)
    try:
        row = registry.set_enabled(ctx.tenant_id, connection_id, body.enabled)
    except ConnectionsError as exc:
        raise conflict(str(exc)) from None
    return {"connection": _view(row, registry, {}, services(request).environ)}


@router.post("/connections/order")
def set_order(body: OrderBody, request: Request) -> dict:
    ctx = member(request, CONNECTION_EDITORS, unsafe=True)
    registry = _registry(request, ctx)
    for cid in body.ids:
        _get(registry, ctx, cid)
    try:
        registry.set_order(ctx.tenant_id, body.ids)
    except ConnectionsError as exc:
        raise conflict(str(exc)) from None
    return {"status": "ordered"}


@router.post("/connections/{connection_id}/secret")
def replace_secret(connection_id: str, body: SecretBody, request: Request) -> dict:
    ctx = member(request, CONNECTION_EDITORS, unsafe=True)
    registry = _registry(request, ctx)
    row = _get(registry, ctx, connection_id)
    vault = services(request).vault
    if vault is None:
        raise ApiError(503, "not_configured", "no vault key is configured; keys cannot be stored")
    if row.secret_ref is None:
        raise conflict("this connection holds no key; remove it and add it again with one")
    try:
        vault.replace(ctx.tenant_id, row.secret_ref, Secret(body.secret.get_secret_value()))
    except ConnectionsError:
        raise bad_request("the key was refused; paste it again without spaces or line breaks") from None
    log.info("connection key replaced tenant=%s connection=%s", ctx.tenant_id, row.id)
    return {"status": "replaced", "has_secret": True}


@router.delete("/connections/{connection_id}")
def remove(connection_id: str, request: Request) -> dict:
    ctx = member(request, CONNECTION_EDITORS, unsafe=True)
    registry = _registry(request, ctx)
    _get(registry, ctx, connection_id)
    try:
        registry.remove(ctx.tenant_id, connection_id)
    except ConnectionsError as exc:
        raise conflict(str(exc)) from None
    return {"status": "removed"}


@router.post("/connections/{connection_id}/test")
def test(connection_id: str, request: Request) -> dict:
    ctx = member(request, CONNECTION_EDITORS, unsafe=True)
    svc = services(request)
    registry = _registry(request, ctx)
    row = _get(registry, ctx, connection_id)
    try:
        health = registry.build(row).health()
        connected, checked_at = bool(health.connected), health.checked_at
    except Exception as exc:  # a broken provider is a down one; the type only, never the message
        log.warning("connection test failed connection=%s error=%s", row.id, type(exc).__name__)
        connected, checked_at = False, svc.clock.now()
    if registry.health is not None:
        registry.health.set_connected(ctx.tenant_id, row.id, connected)
    return {"connection_id": row.id, "health": "connected" if connected else "down",
            "checked_at": checked_at.isoformat()}


def default_environ() -> dict[str, str]:
    """The operator variables the screen reads (origins shown read only). Never a secret."""
    keep = ("THALLO_LOCAL_TEXT_URL",)
    return {k: os.environ[k] for k in keep if k in os.environ}


__all__ = ["NOT_OFFERED", "SETTABLE", "default_environ", "fixed_origin", "router"]
