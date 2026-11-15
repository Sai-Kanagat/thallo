"""`decisive_rows`: a FAIL at a lower attempt than a PASS still decides when it is newer (gate
review, 15 Nov). Rows are built by hand with ids from an advancing clock, so their uuid7 order is
their write order, and handed straight to `review_checks` (no repository filters first)."""
from __future__ import annotations

from datetime import timedelta

from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import CheckLayer, CheckVerdict
from thallo.contracts.ids import new_id
from thallo.contracts.models import CheckResult
from thallo.gate import ContentType, review_checks
from thallo.gate.checks import decisive_rows

CLOCK = FakeClock(FAKE_EPOCH)
TENANT = new_id(CLOCK)
VARIANT = new_id(CLOCK)
SHA = "a" * 64


def row(verdict: CheckVerdict, attempt: int, name: str = "truth.compile") -> CheckResult:
    CLOCK.advance(seconds=1)  # each row later than the last: uuid7 order is write order
    return CheckResult(id=new_id(CLOCK), created_at=CLOCK.now(), tenant_id=TENANT, variant_id=VARIANT,
                       layer=CheckLayer.CODE, check_name=name, verdict=verdict, attempt=attempt, content_sha256=SHA)


def review(rows):
    return review_checks(rows, tenant_id=TENANT, hashes={VARIANT: SHA}, types={VARIANT: {ContentType.TEXT}})


def test_a_fail_at_one_written_after_a_pass_at_three_decides():
    passed = row(CheckVerdict.PASS, 3)
    late = row(CheckVerdict.FAIL, 1)
    assert review([passed, late]).failing == {late.id}
    assert {r.id for r in decisive_rows([passed, late])} == {passed.id, late.id}


def test_the_repair_loop_clears_the_fail():
    first = row(CheckVerdict.FAIL, 1)
    fixed = row(CheckVerdict.PASS, 2)
    r = review([first, fixed])
    assert r.failing == frozenset() and not r.unchecked


def test_a_newer_pass_at_a_lower_attempt_does_not_clear_a_fail():
    fail = row(CheckVerdict.FAIL, 2)
    low_pass = row(CheckVerdict.PASS, 1)
    top_old_pass = row(CheckVerdict.PASS, 3)
    # top_old_pass is newer than the FAIL and at a higher attempt: it clears it.
    assert review([fail, low_pass, top_old_pass]).failing == frozenset()
    # Without it, the only newer PASS is at a lower attempt: the FAIL stands.
    assert review([fail, low_pass]).failing == {fail.id}


def test_an_older_pass_at_a_higher_attempt_does_not_hide_a_fail():
    old_top = row(CheckVerdict.PASS, 5)
    CLOCK.advance(timedelta(minutes=1))
    fail = row(CheckVerdict.FAIL, 4)
    assert review([fail, old_top]).failing == {fail.id}


def test_a_fail_at_the_top_attempt_decides_even_beside_a_newer_pass_at_that_attempt():
    fail = row(CheckVerdict.FAIL, 2)
    same = row(CheckVerdict.PASS, 2)
    assert review([fail, same]).failing == {fail.id}


def test_lower_attempt_flags_still_do_not_decide():
    flag = row(CheckVerdict.FLAG, 1)
    passed = row(CheckVerdict.PASS, 2)
    assert review([flag, passed]).flags == frozenset()
