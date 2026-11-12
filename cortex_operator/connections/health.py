"""The daily health check.

For every enabled connection it calls the provider's `health()`. When the connected flag changes
(or a connection is seen for the first time and is down) it emits `connection.health_changed`.
For every channel account whose token expires within 7 days (or already has) it emits one
`token.expiring` per expiry date; a renewed token re-arms the warning.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cortex_operator.connections.registry import ConnectionRegistry
from cortex_operator.connections.stores import ChannelAccountStore, EventSink, HealthState
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.actors import SystemActor
from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.events import ConnectionHealthPayload, Event, EventName, TokenExpiringPayload
from cortex_operator.contracts.ids import new_id

log = logging.getLogger(__name__)

WARN_BEFORE = timedelta(days=7)
INTERVAL = timedelta(days=1)


@dataclass
class HealthReport:
    checked: int = 0
    changed: list[str] = field(default_factory=list)
    warned: list[str] = field(default_factory=list)


class HealthMonitor:
    def __init__(self, *, clock: Clock, registry: ConnectionRegistry, accounts: ChannelAccountStore,
                 state: HealthState, sink: EventSink) -> None:
        self.clock = clock
        self.registry = registry
        self.accounts = accounts
        self.state = state
        self.sink = sink
        self._actor = SystemActor(id="health-check")

    def _tenants(self) -> list[str]:
        return sorted({c.tenant_id for c in self.registry.connections.list_all()}
                      | {a.tenant_id for a in self.accounts.list_all()})

    def run_if_due(self) -> HealthReport | None:
        """Run the check for every tenant whose last run is a day old or more. None when none was due."""
        now = self.clock.now()
        due = [t for t in self._tenants() if (last := self.state.last_run(t)) is None or now - last >= INTERVAL]
        if not due:
            return None
        return self._run(due)

    def run_once(self) -> HealthReport:
        return self._run(self._tenants())

    def _run(self, tenant_ids: list[str]) -> HealthReport:
        report = HealthReport()
        now = self.clock.now()
        for tenant_id in tenant_ids:
            expiry_by_connection: dict[str, datetime] = {}
            for conn in self.registry.connections.list_for_tenant(tenant_id):
                if not conn.enabled:
                    continue
                report.checked += 1
                health = self._health(conn)
                if health.token_expires_at is not None:
                    expiry_by_connection[conn.id] = health.token_expires_at
                prev = self.state.last_connected(tenant_id, conn.id)
                if (prev is None and not health.connected) or (prev is not None and prev != health.connected):
                    self._emit(tenant_id, EventName.CONNECTION_HEALTH_CHANGED,
                               ConnectionHealthPayload(connection_id=conn.id, capability=conn.capability,
                                                       connected=health.connected))
                    report.changed.append(conn.id)
                self.state.set_connected(tenant_id, conn.id, health.connected)
            for acct in (a for a in self.accounts.list_all() if a.tenant_id == tenant_id):
                expires = acct.token_expires_at or (expiry_by_connection.get(acct.connection_id)
                                                    if acct.connection_id else None)
                if expires is None or expires - now > WARN_BEFORE:
                    continue
                if self.state.warned_expiry(tenant_id, acct.id) == expires:
                    continue
                self._emit(tenant_id, EventName.TOKEN_EXPIRING,
                           TokenExpiringPayload(channel_account_id=acct.id, expires_at=expires))
                self.state.set_warned_expiry(tenant_id, acct.id, expires)
                report.warned.append(acct.id)
            self.state.set_last_run(tenant_id, now)
        return report

    def _health(self, conn) -> c.Health:  # noqa: ANN001
        try:
            return self.registry.build(conn).health()
        except Exception as exc:  # a broken provider is an unhealthy one; log the type only
            log.warning("health check failed connection=%s error=%s", conn.id, type(exc).__name__)
            return c.Health(connected=False, checked_at=self.clock.now(), detail="health check raised")

    def _emit(self, tenant_id: str, name: EventName, payload) -> None:  # noqa: ANN001
        self.sink.emit(Event.build(id=new_id(self.clock), tenant_id=tenant_id, occurred_at=self.clock.now(),
                                   actor=self._actor, name=name, payload=payload))
