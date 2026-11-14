"""The connections registry.

Two parts:

- `ProviderRegistry` knows which providers exist: (capability, name) -> factory. Adding a provider
  is one file under `providers/<capability>/` with a `register(registry)` function. `discover()`
  imports every such module, so no core code changes.
- `ConnectionRegistry` holds each tenant's `Connection` rows and resolves a `ConnectionChoice`:
  the most specific scope first (piece, format, brand, tenant), inside a scope by priority (and for
  text, the exact role before role-less rows), then the broader scopes as fallbacks. The chain ends
  at a manual connection and holds no other manual one: manual never fails, so nothing after it
  could be reached.
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, ValidationError

from thallo.connections.errors import (
    InvalidConnection,
    ManualRequired,
    NoConnection,
    ProviderUnavailable,
    UnknownProvider,
)
from thallo.connections.secret import Secret, find_secret_keys
from thallo.connections.stores import ConnectionStore, HealthState
from thallo.connections.vault import Vault
from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, ConnectionMode, TextRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import Connection

log = logging.getLogger(__name__)

MANUAL = "manual"
ALLOW_MOCK_ENV = "THALLO_ALLOW_MOCK"
MANUAL_PRIORITY = 10_000
_SCOPE_ORDER = (c.OverrideScope.PIECE, c.OverrideScope.FORMAT, c.OverrideScope.BRAND, c.OverrideScope.TENANT)


@dataclass(frozen=True)
class ProviderContext:
    """What a factory gets. The secret is fetched lazily, so manual and mock providers never touch the vault."""

    tenant_id: str
    connection: Connection
    clock: Clock
    vault: Vault | None = field(default=None, repr=False)

    def secret(self) -> Secret | None:
        if self.vault is None:
            return None
        return self.vault.resolve(self.tenant_id, self.connection.secret_ref)


Factory = Callable[[ProviderContext], c.Provider]


class NoSettings(BaseModel):
    """Settings schema for providers that take none (manual, mock)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class ProviderEntry:
    capability: Capability
    name: str
    mode: ConnectionMode
    factory: Factory
    settings_schema: type[BaseModel] | None = None


class ProviderRegistry:
    def __init__(self) -> None:
        self._entries: dict[tuple[Capability, str], ProviderEntry] = {}

    def register(self, capability: Capability, name: str, mode: ConnectionMode, factory: Factory,
                 *, replace: bool = False, settings_schema: type[BaseModel] | None = None) -> None:
        """`settings_schema` is a pydantic model with extra="forbid": the settings a connection of
        this provider may carry. Declare one for every provider that takes settings."""
        key = (capability, name)
        if key in self._entries and not replace:
            raise InvalidConnection(f"provider {capability.value}/{name} is already registered")
        self._entries[key] = ProviderEntry(capability, name, mode, factory, settings_schema)

    def get(self, capability: Capability, name: str) -> ProviderEntry:
        try:
            return self._entries[(capability, name)]
        except KeyError:
            raise UnknownProvider(f"no provider {capability.value}/{name}") from None

    def names(self, capability: Capability) -> list[str]:
        return sorted(n for (cap, n) in self._entries if cap is capability)

    def discover(self, package: str = "thallo.providers") -> ProviderRegistry:
        """Import providers/<capability>/<module>.py and call its register(registry), if it has one."""
        for cap in Capability:
            pkg_name = f"{package}.{cap.value}"
            try:
                pkg = importlib.import_module(pkg_name)
            except ModuleNotFoundError as exc:
                if exc.name == pkg_name:
                    continue
                raise
            for info in pkgutil.iter_modules(pkg.__path__):
                mod = importlib.import_module(f"{pkg_name}.{info.name}")
                hook = getattr(mod, "register", None)
                if callable(hook):
                    hook(self)
        return self

    @classmethod
    def with_discovered(cls) -> ProviderRegistry:
        return cls().discover()


@dataclass(frozen=True)
class Served:
    connection_id: str
    provider: str
    mode: ConnectionMode
    result: c.RunResult
    skipped: tuple[tuple[str, str], ...] = ()  # (connection id, reason)


class CostOverCapAfterRun(c.CostCapExceeded):
    """The provider ran and then reported a cost over the cap. The call was made and billed, so
    the served result travels with the refusal for the caller's cost ledger."""

    def __init__(self, message: str, served: Served) -> None:
        super().__init__(message)
        self.served = served


class ConnectionRegistry:
    def __init__(self, *, clock: Clock, providers: ProviderRegistry, connections: ConnectionStore,
                 vault: Vault | None = None, health: HealthState | None = None,
                 mock_tenants: Collection[str] = (), environ: Mapping[str, str] | None = None) -> None:
        """Mock connections are only allowed for the demo or test tenants in `mock_tenants`, or
        everywhere when THALLO_ALLOW_MOCK=1. A real tenant can never silently "send" through a mock."""
        self.mock_tenants = frozenset(mock_tenants)
        self._allow_mock_everywhere = (os.environ if environ is None else environ).get(ALLOW_MOCK_ENV) == "1"
        self.clock = clock
        self.providers = providers
        self.connections = connections
        self.vault = vault
        self.health = health

    def mock_allowed(self, tenant_id: str) -> bool:
        return self._allow_mock_everywhere or tenant_id in self.mock_tenants

    # ------------------------------------------------------------------ configuring

    def ensure_manual(self, tenant_id: str, capability: Capability | None = None) -> list[Connection]:
        """Make sure the tenant-level manual connection exists for each capability. Idempotent."""
        made: list[Connection] = []
        rows = self.connections.list_for_tenant(tenant_id)
        for cap in ([capability] if capability else list(Capability)):
            if any(r for r in rows if r.capability is cap and _is_floor(r)):
                continue
            if any(r for r in rows if r.capability is cap and _is_tenant_manual(r) and r.enabled is False):
                log.warning("tenant manual connection for %s is disabled in the store; adding a floor", cap.value)
            entry = self.providers.get(cap, MANUAL)
            row = Connection(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=tenant_id,
                             capability=cap, provider=MANUAL, mode=entry.mode, scope="tenant",
                             priority=MANUAL_PRIORITY)
            self.connections.put(row)
            made.append(row)
        return made

    def add(self, tenant_id: str, capability: Capability, provider: str, *,
            scope: c.OverrideScope = c.OverrideScope.TENANT, scope_ref: str | None = None,
            role: TextRole | None = None, model: str | None = None, priority: int | None = None,
            settings: dict | None = None, secret: str | Secret | None = None, enabled: bool = True) -> Connection:
        entry = self.providers.get(capability, provider)
        settings = dict(settings or {})
        if entry.mode is ConnectionMode.MOCK and not self.mock_allowed(tenant_id):
            raise InvalidConnection("mock connections are only for demo or test tenants")
        if provider == MANUAL and scope is c.OverrideScope.TENANT and (not enabled or role is not None):
            raise InvalidConnection("the tenant manual connection must be enabled and have no role")
        if (scope is c.OverrideScope.TENANT) != (scope_ref is None):
            raise InvalidConnection("tenant scope takes no scope_ref; every other scope needs one")
        if role is not None and capability is not Capability.TEXT:
            raise InvalidConnection("only text connections are per role")
        if entry.settings_schema is not None:
            try:
                parsed = entry.settings_schema.model_validate(settings)
            except ValidationError as exc:
                fields = sorted({".".join(map(str, e["loc"])) for e in exc.errors()})
                raise InvalidConnection(f"settings do not match {provider}'s schema: {fields}") from None
            settings = parsed.model_dump(mode="json")
        elif settings and entry.mode in (ConnectionMode.MANUAL, ConnectionMode.MOCK):
            raise InvalidConnection(f"{provider} takes no settings")
        bad = find_secret_keys(settings)
        if bad:
            raise InvalidConnection(f"settings may not hold secrets ({bad}); pass secret= and the vault stores it")
        group = self._group(tenant_id, capability, scope, scope_ref, role)
        if any(r.provider == provider for r in group):
            raise InvalidConnection(f"{provider} is already configured for this {capability.value} scope")
        if priority is None:
            real = [r.priority for r in group if r.priority < MANUAL_PRIORITY]
            priority = (max(real) + 1) if real else 0
        if secret is not None and self.vault is None:
            raise InvalidConnection("no vault configured, cannot store a secret")
        row = Connection(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=tenant_id,
                         capability=capability, provider=provider, mode=entry.mode, model=model, role=role,
                         scope=scope.value, scope_ref=scope_ref, priority=priority, enabled=enabled,
                         settings=settings)  # validated before anything is written to the vault
        if secret is not None and self.vault is not None:
            ref = self.vault.put(tenant_id, secret)
            row = row.model_copy(update={"secret_ref": ref})
            try:
                self.connections.put(row)
            except BaseException:
                self.vault.delete(tenant_id, ref)
                raise
        else:
            self.connections.put(row)
        self.ensure_manual(tenant_id, capability)
        return row

    def set_order(self, tenant_id: str, ordered_ids: list[str]) -> None:
        """Set priorities 0..n for connections of one capability, scope and role. First is the default."""
        rows = [self._get(tenant_id, i) for i in ordered_ids]
        keys = {(r.capability, r.scope, r.scope_ref, r.role) for r in rows}
        if len(keys) != 1 or len(set(ordered_ids)) != len(ordered_ids):
            raise InvalidConnection("set_order takes distinct connections of one capability, scope and role")
        if any(_is_tenant_manual(r) for r in rows):
            raise ManualRequired("the tenant manual connection is always last and is not reordered")
        self.connections.put_many([r.model_copy(update={"priority": i}) for i, r in enumerate(rows)])  # atomic

    def set_enabled(self, tenant_id: str, connection_id: str, enabled: bool) -> Connection:
        row = self._get(tenant_id, connection_id)
        if _is_tenant_manual(row) and not enabled:
            raise ManualRequired("the tenant manual connection cannot be disabled")
        new = row.model_copy(update={"enabled": enabled})
        self.connections.put(new)
        return new

    def remove(self, tenant_id: str, connection_id: str) -> None:
        row = self._get(tenant_id, connection_id)
        if _is_tenant_manual(row):
            raise ManualRequired("the tenant manual connection cannot be removed")
        self.connections.delete(tenant_id, connection_id)
        if row.secret_ref and self.vault:
            self.vault.delete(tenant_id, row.secret_ref)

    # ------------------------------------------------------------------ resolving

    def resolve(self, tenant_id: str, capability: Capability, *, role: TextRole | None = None,
                brand_id: str | None = None, format: str | None = None,  # noqa: A002
                piece_id: str | None = None) -> c.ConnectionChoice:
        self.ensure_manual(tenant_id, capability)
        refs = {c.OverrideScope.PIECE: piece_id, c.OverrideScope.FORMAT: format,
                c.OverrideScope.BRAND: brand_id, c.OverrideScope.TENANT: None}
        mocks_ok = self.mock_allowed(tenant_id)
        rows = [r for r in self.connections.list_for_tenant(tenant_id)
                if r.capability is capability and r.enabled and (mocks_ok or r.mode is not ConnectionMode.MOCK)]
        chain: list[Connection] = []
        top: tuple[c.OverrideScope, str | None] | None = None
        for scope in _SCOPE_ORDER:
            ref = refs[scope]
            if scope is not c.OverrideScope.TENANT and ref is None:
                continue
            level = [r for r in rows if r.scope == scope.value and r.scope_ref == ref
                     and (r.role is None or r.role == role)]
            level.sort(key=lambda r: (r.role is None, r.priority, r.created_at))
            if level and top is None:
                top = (scope, ref)
            chain.extend(level)
        ids: list[str] = []
        for r in chain:
            if r.id in ids:
                continue
            ids.append(r.id)
            if r.mode is ConnectionMode.MANUAL:
                break  # manual never fails; anything after it is unreachable
        else:
            floor = next((r for r in rows if _is_floor(r)), None)
            if floor is None:
                raise ManualRequired(f"tenant has no usable manual connection for {capability.value}")
            ids.append(floor.id)
        scope, ref = top or (c.OverrideScope.TENANT, None)
        return c.ConnectionChoice(capability=capability, role=role, ordered_connection_ids=tuple(ids),
                                  scope=scope, scope_ref=ref)

    def build(self, connection: Connection) -> c.Provider:
        entry = self.providers.get(connection.capability, connection.provider)
        return entry.factory(ProviderContext(connection.tenant_id, connection, self.clock, self.vault))

    # ------------------------------------------------------------------ running

    def run(self, tenant_id: str, request: c.Request, *, brand_id: str | None = None,
            format: str | None = None, piece_id: str | None = None) -> Served:  # noqa: A002
        """Run a request down the chain.

        The registry does not trust a provider about money: before each provider it calls
        `estimate()` and `check_cap()` itself, and it checks the cost on the result afterwards.
        A connection is skipped only on that pre-check (over the cap, `ProviderUnavailable` from
        `estimate()`) or when it was last seen unhealthy. Anything raised from inside a provider's
        `run()`, `CostCapExceeded` and `ProviderUnavailable` included, stops the chain: the provider
        may already have reached a third party, and a second send could duplicate it. A request
        needs an idempotency key so a retry by the caller is safe."""
        if not request.idempotency_key:
            raise InvalidConnection("a request passed down the chain needs an idempotency_key")
        role = request.role if isinstance(request, c.TextRequest) else None
        choice = self.resolve(tenant_id, request.capability, role=role, brand_id=brand_id, format=format,
                              piece_id=piece_id)
        skipped: list[tuple[str, str]] = []
        for cid in choice.ordered_connection_ids:
            row = self._get(tenant_id, cid)
            down = self.health is not None and self.health.last_connected(tenant_id, cid) is False
            if down and row.mode is not ConnectionMode.MANUAL:
                skipped.append((cid, "unhealthy"))
                continue
            provider = self.build(row)
            try:
                c.check_cap(provider.estimate(request), request)
            except ProviderUnavailable:
                skipped.append((cid, "unavailable"))
                continue
            except c.CostCapExceeded:
                skipped.append((cid, "over_cost_cap"))
                continue
            result = provider.run(request)
            served = Served(cid, row.provider, row.mode, result, tuple(skipped))
            if request.cost_cap_eur is not None and result.cost_eur > request.cost_cap_eur:
                raise CostOverCapAfterRun(f"connection {cid} reported a cost over the cap", served)
            return served
        raise NoConnection(f"no connection could serve {request.capability.value}: {skipped}")

    def estimate_chain(self, tenant_id: str, request: c.Request, **scope: str | None) -> Iterable[tuple[str, Decimal]]:
        role = request.role if isinstance(request, c.TextRequest) else None
        choice = self.resolve(tenant_id, request.capability, role=role, **scope)  # type: ignore[arg-type]
        for cid in choice.ordered_connection_ids:
            yield cid, self.build(self._get(tenant_id, cid)).estimate(request).cost_eur

    # ------------------------------------------------------------------ internals

    def _get(self, tenant_id: str, connection_id: str) -> Connection:
        row = self.connections.get(tenant_id, connection_id)
        if row is None:
            raise InvalidConnection("no such connection for this tenant")
        return row

    def _group(self, tenant_id: str, capability: Capability, scope: c.OverrideScope, scope_ref: str | None,
               role: TextRole | None) -> list[Connection]:
        return [r for r in self.connections.list_for_tenant(tenant_id)
                if r.capability is capability and r.scope == scope.value and r.scope_ref == scope_ref
                and r.role == role]


def _is_tenant_manual(row: Connection) -> bool:
    return row.mode is ConnectionMode.MANUAL and row.scope == "tenant" and row.provider == MANUAL


def _is_floor(row: Connection) -> bool:
    """The row that ends every chain: tenant-level manual, enabled, and not tied to a text role."""
    return _is_tenant_manual(row) and row.enabled and row.role is None
