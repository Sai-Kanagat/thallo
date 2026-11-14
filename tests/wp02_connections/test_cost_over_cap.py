"""Security fix 4 (HANDOFF, 14 Nov): a result over the cost cap after the call was billed. It
used to be a `CostCapExceeded`, so a caller that caught only that ("refused, nothing spent")
dropped a paid call. Now it is its own exception: no handler for `CostCapExceeded` or for
`ConnectionsError` can absorb it, it carries the served result, and the registry logs it.

The registry has no ledger by design (the cost ledger sits above every connection and is
written by the caller in its own transaction), so the guarantee tested here is: whichever
handler style sits in between, the billed call reaches exactly one ledger row, written by the
code that handles `CostOverCapAfterRun` by name."""
from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from thallo.connections import ConnectionsError
from thallo.connections.registry import ConnectionRegistry, CostOverCapAfterRun, Served
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability

from .conftest import T1
from .test_registry import register, req

TEXT = Capability.TEXT


def capped(key="idem-cap"):
    return req(key).model_copy(update={"cost_cap_eur": Decimal(1)})


def test_not_a_cost_cap_refusal_and_not_a_connection_error():
    assert not issubclass(CostOverCapAfterRun, c.CostCapExceeded)
    assert not issubclass(CostOverCapAfterRun, ConnectionsError)
    assert not issubclass(CostOverCapAfterRun, RuntimeError)  # CostCapExceeded's own base


class Ledger:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Decimal]] = []

    def record(self, served: Served) -> None:
        self.rows.append((served.connection_id, served.result.cost_eur))


def catches_the_subclass(registry: ConnectionRegistry, ledger: Ledger) -> str:
    try:
        served = registry.run(T1, capped())
    except CostOverCapAfterRun as exc:
        ledger.record(exc.served)
        return "stopped"
    ledger.record(served)
    return "served"


def catches_the_parent(registry: ConnectionRegistry, ledger: Ledger) -> str:
    """A future caller written against the old hierarchy: treats a cap refusal as nothing spent."""
    try:
        served = registry.run(T1, capped())
    except c.CostCapExceeded:
        return "refused, nothing spent"
    except ConnectionsError:
        return "provider error"
    ledger.record(served)
    return "served"


def catches_nothing(registry: ConnectionRegistry, ledger: Ledger) -> str:
    served = registry.run(T1, capped())
    ledger.record(served)
    return "served"


def job_runner(caller, registry: ConnectionRegistry, ledger: Ledger) -> str:
    """The outermost handler: whatever the caller in between did, a billed call that escaped it
    is recorded here, once."""
    try:
        return caller(registry, ledger)
    except CostOverCapAfterRun as exc:
        ledger.record(exc.served)
        return "stopped by the runner"


@pytest.mark.parametrize(("caller", "outcome"), [(catches_the_subclass, "stopped"),
                                                 (catches_the_parent, "stopped by the runner"),
                                                 (catches_nothing, "stopped by the runner")])
def test_exactly_one_ledger_row_whatever_the_caller_catches(registry, providers, caller, outcome, caplog):
    register(providers, "liar", "overspend")  # reports 9 EUR against a 1 EUR cap, after the call
    row = registry.add(T1, TEXT, "liar")
    ledger = Ledger()
    with caplog.at_level(logging.WARNING, logger="thallo.connections.registry"):
        assert job_runner(caller, registry, ledger) == outcome
    assert ledger.rows == [(row.id, Decimal(9))]
    assert any(row.id in r.getMessage() and "cap" in r.getMessage() for r in caplog.records)


def test_a_parent_handler_never_swallows_it(registry, providers):
    register(providers, "liar", "overspend")
    registry.add(T1, TEXT, "liar")
    with pytest.raises(CostOverCapAfterRun) as exc:
        catches_the_parent(registry, Ledger())
    assert exc.value.cost_eur == Decimal(9) and exc.value.served.result.cost_eur == Decimal(9)


def test_a_refusal_before_spending_is_still_a_cost_cap_exceeded(registry, providers):
    """The pre-call refusal path is unchanged: a provider whose run() refuses on cost raises the
    parent, and nothing was billed."""
    register(providers, "strict", "run_cap")
    registry.add(T1, TEXT, "strict")
    with pytest.raises(c.CostCapExceeded) as exc:
        registry.run(T1, capped())
    assert not isinstance(exc.value, CostOverCapAfterRun)
