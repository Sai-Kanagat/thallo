"""Copy production keeps a grader's billed cost, whether the grader answers or raises."""
from __future__ import annotations

from decimal import Decimal

from tests.wp06_text.test_copy import CLEAN, SOURCE, Script
from tests.wp06_text.test_copy import make_registry as copy_registry
from thallo.contracts.enums import Capability, Channel, CheckLayer, CheckVerdict
from thallo.contracts.ids import new_id
from thallo.contracts.models import CostEntry
from thallo.contracts.testing.samples import T
from thallo.copy import CopyStatus, produce_copy

from .conftest import NO_LINKS


class BilledGrader:
    def __init__(self, clock, *, raises: bool) -> None:
        self.clock, self.raises, self.pending = clock, raises, []

    def grade(self, text, brief):
        self.pending.append(CostEntry(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=T,
                                      capability=Capability.TEXT, provider="claude", cost_eur=Decimal("0.01"),
                                      run_ref="grade", occurred_at=self.clock.now()))
        if self.raises:
            raise RuntimeError("vendor broke after billing")
        return 1.0, "fine"

    def drain_costs(self):
        out, self.pending = self.pending, []
        return out


def _produce(clock, vault, tenant, brief, price, grader):
    reg = copy_registry(clock, vault, Script(CLEAN), prices=("2", "10"))
    return produce_copy(registry=reg, tenant=tenant, brief=brief, claims={"C-0001": price}, sources=[SOURCE],
                        channel=Channel.INSTAGRAM, rule_version=1, clock=clock, candidates=1, grader=grader,
                        resolver=NO_LINKS, non_links=())


def test_a_raising_grader_never_swallows_its_billed_cost(clock, vault, tenant, brief, price):
    out = _produce(clock, vault, tenant, brief, price, BilledGrader(clock, raises=True))
    assert out.status is CopyStatus.READY_FOR_CHECKS  # a grader never fails a build
    flags = [r for r in out.check_results if r.check_name == "copy.grader.not_run"]
    assert len(flags) == 1 and flags[0].verdict is CheckVerdict.FLAG and flags[0].layer is CheckLayer.GRADER
    assert not any(r.verdict is CheckVerdict.FAIL for r in out.check_results)
    assert sum(1 for e in out.cost_entries if e.run_ref == "grade") == 1
    assert out.spent_eur == sum(e.cost_eur for e in out.cost_entries)


def test_an_answering_grader_cost_is_kept(clock, vault, tenant, brief, price):
    out = _produce(clock, vault, tenant, brief, price, BilledGrader(clock, raises=False))
    assert out.status is CopyStatus.READY_FOR_CHECKS
    assert sum(1 for e in out.cost_entries if e.run_ref == "grade") == 1
