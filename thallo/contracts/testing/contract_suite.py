"""The contract every provider must pass, manual and mock included.

Later packages call `assert_provider_contract(provider)` from their own tests:

    def test_my_provider():
        assert_provider_contract(MyProvider(...), clock=FakeClock(FAKE_EPOCH))
"""
from __future__ import annotations

from decimal import Decimal

from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.samples import over_cap, sample_requests


class ContractFailure(AssertionError):
    pass


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise ContractFailure(msg)


def assert_provider_contract(provider: c.Provider, clock: Clock,
                             requests: list[c.Request] | None = None) -> None:
    cap = provider.capability
    _expect(isinstance(cap, Capability), "capability must be a Capability")
    _expect(isinstance(provider, c.Provider), "must implement describe, health, estimate, run, mock")

    info = provider.describe()
    _expect(isinstance(info, c.ProviderInfo), "describe() returns ProviderInfo")
    _expect(info.capability is cap, "describe().capability matches")

    health = provider.health()
    _expect(isinstance(health, c.Health), "health() returns Health")
    _expect(health.checked_at.tzinfo is not None, "health().checked_at is timezone-aware")

    reqs = requests if requests is not None else sample_requests()[cap]
    for req in reqs:
        est = provider.estimate(req)
        _expect(isinstance(est, c.Estimate) and est.cost_eur >= 0 and est.seconds >= 0, "estimate is non-negative")

        res = provider.run(req)
        _expect(isinstance(res, c.RunResult), "run() returns RunResult")
        _expect(isinstance(res.provenance, c.Provenance), "run() records provenance")
        _expect(res.cost_eur >= Decimal(0), "cost is non-negative")
        _expect(res.provenance.made_at.tzinfo is not None, "provenance time is timezone-aware")

        try:
            provider.run(over_cap(req))
        except c.CostCapExceeded:
            pass
        else:
            raise ContractFailure("run() must refuse when the estimate is over cost_cap_eur")

    wrong = next(r for k, rs in sample_requests().items() if k is not cap for r in rs)
    for call in (provider.estimate, provider.run):
        try:
            call(wrong)
        except c.CapabilityMismatch:
            continue
        raise ContractFailure(f"{call.__name__}() must refuse another capability's request")

    double = provider.mock()
    _expect(double.capability is cap, "mock() serves the same capability")
    dinfo = double.describe()
    _expect(dinfo.mode is ConnectionMode.MOCK, "mock() reports mode MOCK")
    _expect(not dinfo.sends_externally, "mock() never sends externally")
    for req in reqs:
        a, b = double.run(req), double.run(req)
        _expect(a.output == b.output, "mock() is deterministic")
        _expect(a.cost_eur == 0, "mock() costs nothing")
