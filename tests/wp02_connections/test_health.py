from __future__ import annotations

from datetime import timedelta

import pytest

from cortex_operator.connections import (
    HealthMonitor,
    InMemoryChannelAccountStore,
    InMemoryHealthState,
    ListEventSink,
)
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, Channel, ConnectionMode
from cortex_operator.contracts.events import EventName
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import ChannelAccount
from cortex_operator.contracts.testing.mocks import MockProvider

from .conftest import T1, T2

TEXT = Capability.TEXT


class Flaky(MockProvider):
    up = True
    expires = None
    raises = False

    def __init__(self, ctx):
        super().__init__(TEXT, ctx.clock, name=ctx.connection.provider)

    def health(self):
        if type(self).raises:
            raise RuntimeError("secret-looking detail sk-test-LEAK")
        return c.Health(connected=type(self).up, checked_at=self.clock.now(), token_expires_at=type(self).expires)


@pytest.fixture
def flaky(providers):
    cls = type("F", (Flaky,), {"up": True, "expires": None, "raises": False})
    providers.register(TEXT, "flaky", ConnectionMode.HOSTED, cls)
    return cls


@pytest.fixture
def rig(registry, clock, flaky):
    accounts, state, sink = InMemoryChannelAccountStore(), InMemoryHealthState(), ListEventSink()
    mon = HealthMonitor(clock=clock, registry=registry, accounts=accounts, state=state, sink=sink)
    return mon, accounts, sink, registry


def account(clock, tenant, expires, connection_id=None):
    return ChannelAccount(id=new_id(clock), created_at=clock.now(), tenant_id=tenant, channel=Channel.FACEBOOK,
                          handle="@x", token_expires_at=expires, connection_id=connection_id)


def names(sink):
    return [e.name for e in sink.events]


def test_no_event_when_healthy_and_unchanged(rig):
    mon, _, sink, reg = rig
    reg.add(T1, TEXT, "flaky")
    mon.run_once()
    assert sink.events == []
    mon.run_once()
    assert sink.events == []


def test_event_when_health_changes_both_ways(rig, flaky):
    mon, _, sink, reg = rig
    row = reg.add(T1, TEXT, "flaky")
    mon.run_once()
    flaky.up = False
    rep = mon.run_once()
    assert rep.changed == [row.id]
    flaky.up = True
    mon.run_once()
    evs = sink.events
    assert names(sink) == [EventName.CONNECTION_HEALTH_CHANGED] * 2
    assert [e.payload["connected"] for e in evs] == [False, True]
    assert evs[0].payload["connection_id"] == row.id and evs[0].tenant_id == T1
    assert evs[0].actor.kind.value == "system"


def test_first_sight_of_a_down_connection_is_reported(rig, flaky):
    mon, _, sink, reg = rig
    flaky.up = False
    reg.add(T1, TEXT, "flaky")
    mon.run_once()
    assert names(sink) == [EventName.CONNECTION_HEALTH_CHANGED]


def test_a_provider_that_raises_counts_as_down_and_leaks_nothing(rig, flaky):
    mon, _, sink, reg = rig
    reg.add(T1, TEXT, "flaky")
    mon.run_once()
    flaky.raises = True
    mon.run_once()
    assert [e.payload["connected"] for e in sink.events] == [False]
    assert "LEAK" not in repr(sink.events)


def test_disabled_connections_are_not_checked(rig, flaky):
    mon, _, sink, reg = rig
    row = reg.add(T1, TEXT, "flaky", enabled=False)
    flaky.up = False
    assert mon.run_once().checked == 1  # only the manual floor
    assert row.id not in mon.state._connected


def test_token_warning_seven_days_before_not_earlier(rig, clock):
    mon, accounts, sink, _ = rig
    acct = account(clock, T1, clock.now() + timedelta(days=8))
    accounts.put(acct)
    mon.run_once()
    assert sink.events == []
    clock.advance(timedelta(hours=23))
    mon.run_once()
    assert sink.events == []
    clock.advance(timedelta(hours=2))  # now 6d 23h left... within 7 days
    mon.run_once()
    assert names(sink) == [EventName.TOKEN_EXPIRING]
    assert sink.events[0].payload["channel_account_id"] == acct.id


def test_token_warning_exactly_at_seven_days(rig, clock):
    mon, accounts, sink, _ = rig
    accounts.put(account(clock, T1, clock.now() + timedelta(days=7)))
    mon.run_once()
    assert names(sink) == [EventName.TOKEN_EXPIRING]


def test_token_warning_once_per_expiry_and_rearms_on_renewal(rig, clock):
    mon, accounts, sink, _ = rig
    acct = account(clock, T1, clock.now() + timedelta(days=3))
    accounts.put(acct)
    for _ in range(3):
        mon.run_once()
        clock.advance(days=1)
    assert names(sink) == [EventName.TOKEN_EXPIRING]
    accounts.put(acct.model_copy(update={"token_expires_at": clock.now() + timedelta(days=60)}))
    mon.run_once()
    assert len(sink.events) == 1
    accounts.put(acct.model_copy(update={"token_expires_at": clock.now() + timedelta(days=2)}))
    mon.run_once()
    assert len(sink.events) == 2


def test_already_expired_token_is_flagged(rig, clock):
    mon, accounts, sink, _ = rig
    accounts.put(account(clock, T1, clock.now() - timedelta(days=1)))
    mon.run_once()
    assert names(sink) == [EventName.TOKEN_EXPIRING]


def test_no_expiry_no_warning(rig, clock):
    mon, accounts, sink, _ = rig
    accounts.put(account(clock, T1, None))
    mon.run_once()
    assert sink.events == []


def test_expiry_can_come_from_the_linked_connections_health(rig, flaky, clock):
    mon, accounts, sink, reg = rig
    row = reg.add(T1, TEXT, "flaky")
    flaky.expires = clock.now() + timedelta(days=2)
    accounts.put(account(clock, T1, None, connection_id=row.id))
    mon.run_once()
    assert names(sink) == [EventName.TOKEN_EXPIRING]


def test_events_stay_in_their_own_tenant(rig, clock, flaky):
    mon, accounts, sink, reg = rig
    flaky.up = False
    reg.add(T1, TEXT, "flaky")
    reg.add(T2, TEXT, "flaky")
    accounts.put(account(clock, T2, clock.now() + timedelta(days=1)))
    mon.run_once()
    by_tenant = {e.tenant_id for e in sink.events}
    assert by_tenant == {T1, T2}
    assert all(e.payload["connection_id"] in {r.id for r in reg.connections.list_for_tenant(e.tenant_id)}
               for e in sink.events if e.name is EventName.CONNECTION_HEALTH_CHANGED)


def test_runs_daily_only(rig, clock):
    mon, _, _, _ = rig
    assert mon.run_if_due() is not None
    assert mon.run_if_due() is None
    clock.advance(hours=23)
    assert mon.run_if_due() is None
    clock.advance(hours=1)
    assert mon.run_if_due() is not None
