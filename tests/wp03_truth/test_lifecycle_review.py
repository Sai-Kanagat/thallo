"""Claim lifecycle fixes from the review (O1 to O3) and price formatting (O5)."""
import pytest

from tests.wp03_truth.conftest import DAY, SYSTEM
from thallo.contracts.actors import HumanActor, HumanOnlyViolation, SystemActor
from thallo.contracts.enums import ClaimKind, Language
from thallo.contracts.ids import new_id
from thallo.contracts.states import ClaimState
from thallo.truth import ClaimError, change_claim, expire_due, new_candidate, render_claim, verify_claim

EN, IT, TA = Language.EN, Language.IT, Language.TA


# ---------------------------------------------------------------- O1: change_claim is human-only

def test_o1_a_machine_cannot_change_a_claim(make, clock, verifier, repo):
    price = make("C-0101", ClaimKind.PRICE, "100", "EUR")
    with pytest.raises(HumanOnlyViolation):
        change_claim(price, actor=SystemActor(id="writer-model"), verifier=verifier, clock=clock, repo=repo, value="1")


def test_o1_another_tenant_or_a_dead_session_cannot_change_a_claim(make, clock, human, verifier, repo):
    price = make("C-0101", ClaimKind.PRICE, "100", "EUR")
    for actor in (HumanActor(id=human.id, tenant_id=new_id(clock), session_id="live"),
                  HumanActor(id=human.id, tenant_id=human.tenant_id, session_id="stolen")):
        with pytest.raises(HumanOnlyViolation):
            change_claim(price, actor=actor, verifier=verifier, clock=clock, repo=repo, value="2")


# ---------------------------------------------------------------- O2: void even when expired; latest only

def test_o2_change_voids_an_expired_row(make, clock, human, verifier, repo):
    price = make("C-0102", ClaimKind.PRICE, "5", "EUR")
    clock.advance(31 * DAY)
    [(expired, _)] = expire_due([price], clock)
    repo.save(expired)
    ch = change_claim(expired, actor=human, verifier=verifier, clock=clock, repo=repo, value="6")
    assert ch.previous.state is ClaimState.VOIDED and ch.current.version == 2


def test_o2_verify_and_change_refuse_an_older_version(make, clock, human, verifier, repo):
    v1 = make("C-0001", ClaimKind.NUMBER, "3", "t")
    ch = change_claim(v1, actor=human, verifier=verifier, clock=clock, repo=repo, value="4")
    repo.save(ch.previous)
    repo.save(ch.current)
    with pytest.raises(ClaimError, match="latest version"):
        change_claim(v1, actor=human, verifier=verifier, clock=clock, repo=repo, value="5")
    with pytest.raises(ClaimError, match="latest version"):
        verify_claim(v1.model_copy(update={"state": ClaimState.EXPIRED}), human, verifier=verifier, clock=clock,
                     repo=repo, expires_at=clock.now() + DAY)
    v2, _ = verify_claim(ch.current, human, verifier=verifier, clock=clock, repo=repo)
    assert v2.version == 2


# ---------------------------------------------------------------- O3: expiry reason and ceiling

def test_o3_a_long_expiry_needs_a_real_reason_and_has_a_ceiling(clock, tenant, human, verifier, repo):
    price, _ = new_candidate(tenant_id=tenant, code="C-0102", kind=ClaimKind.PRICE, value="5", unit="EUR",
                             clock=clock, actor=SYSTEM, evidence="e")
    for reason in (".", "annual", "x" * 14):
        with pytest.raises(ClaimError, match="15 characters"):
            verify_claim(price, human, verifier=verifier, clock=clock, repo=repo, expires_at=clock.now() + 180 * DAY,
                         expiry_reason=reason)
    with pytest.raises(ClaimError, match="365 days"):
        verify_claim(price, human, verifier=verifier, clock=clock, repo=repo, expires_at=clock.now() + 3650 * DAY,
                     expiry_reason="Signed annual dealer contract on file")
    ok, _ = verify_claim(price, human, verifier=verifier, clock=clock, repo=repo, expires_at=clock.now() + 365 * DAY,
                         expiry_reason="Signed annual dealer contract on file")
    assert ok.expires_at == clock.now() + 365 * DAY


# ---------------------------------------------------------------- O5: minor units and grouping

@pytest.mark.parametrize(("value", "unit", "language", "out"), [
    ("1234567.5", "EUR", EN, "€1,234,567.50"), ("1234567.5", "EUR", IT, "1.234.567,50 €"),
    ("1234567.5", "EUR", TA, "€1,234,567.50"), ("1234567.5", "INR", EN, "₹12,34,567.50"),
    ("1234567.5", "INR", IT, "12.34.567,50 ₹"), ("1234567", "JPY", EN, "JPY 1,234,567"),
    ("1234.5", "KWD", EN, "KWD 1,234.500"), ("100", "EUR", EN, "€100"),
])
def test_o5_prices_follow_iso_4217_minor_units(make, value, unit, language, out):
    claim = make("C-0001", ClaimKind.PRICE, value, unit)
    assert render_claim(claim, language, default_language=EN) == out


@pytest.mark.parametrize(("value", "unit"), [("1234.5", "JPY"), ("9.999", "EUR"), ("1.2345", "KWD")])
def test_o5_too_many_decimals_for_the_currency_are_refused(clock, tenant, value, unit):
    with pytest.raises(ClaimError, match="decimal"):
        new_candidate(tenant_id=tenant, code="C-0001", kind=ClaimKind.PRICE, value=value, unit=unit, clock=clock,
                      actor=SYSTEM, evidence="e")


def test_o5_numbers_use_indian_grouping_only_for_inr(make):
    claim = make("C-0001", ClaimKind.NUMBER, "1234567", "km")
    assert render_claim(claim, TA, default_language=EN) == "1,234,567 km"
