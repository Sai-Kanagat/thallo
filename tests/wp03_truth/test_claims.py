import pytest

from cortex_operator.contracts.actors import AgentActor, HumanActor, HumanOnlyViolation, SystemActor
from cortex_operator.contracts.enums import ClaimKind
from cortex_operator.contracts.events import ClaimChangedPayload, EventName
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.states import ClaimState, IllegalTransition
from cortex_operator.truth import (
    ClaimError,
    ClaimRepository,
    InMemoryClaimRepository,
    change_claim,
    expire_due,
    new_candidate,
    next_code,
    reject_claim,
    verify_claim,
)
from tests.wp03_truth.conftest import DAY, SYSTEM


def candidate(clock, tenant, kind=ClaimKind.NUMBER, value="3.5", unit="t", **kw):
    kw.setdefault("evidence", "brochure page 2")
    claim, event = new_candidate(tenant_id=tenant, code="C-0001", kind=kind, value=value, unit=unit, clock=clock,
                                 actor=SYSTEM, **kw)
    return claim, event


def test_candidate_is_typed_and_emits_event(clock, tenant):
    claim, event = candidate(clock, tenant)
    assert claim.state is ClaimState.CANDIDATE and claim.evidence_sha256
    assert event.name is EventName.CLAIM_CANDIDATE_ADDED and event.payload["code"] == "C-0001"


@pytest.mark.parametrize(("kind", "value", "unit"), [
    (ClaimKind.NUMBER, "3,5", "t"), (ClaimKind.NUMBER, "\u0bef", None), (ClaimKind.PRICE, "10", None),
    (ClaimKind.DATE, "05/01/2026", None), (ClaimKind.NAME, " Acme", None), (ClaimKind.NUMBER, "007", None),
    (ClaimKind.NUMBER, "-0", None), (ClaimKind.NUMBER, "-0.0", None),
    (ClaimKind.DATE, "\u0be8\u0be6\u0be8\u0bec", None),
])
def test_values_must_be_canonical(clock, tenant, kind, value, unit):
    with pytest.raises(ClaimError):
        candidate(clock, tenant, kind, value, unit)


def test_next_code():
    assert next_code([]) == "C-0001"
    assert next_code(["C-0009", "C-0012", "junk"]) == "C-0013"


@pytest.mark.parametrize("actor", [SystemActor(id="cron"), AgentActor(id="writer", model_id="m")])
def test_machines_cannot_verify(clock, tenant, verifier, actor):
    claim, _ = candidate(clock, tenant)
    with pytest.raises(HumanOnlyViolation):
        verify_claim(claim, actor, verifier=verifier, clock=clock)


def test_unverified_session_and_other_tenant_cannot_verify(clock, tenant, human, verifier):
    claim, _ = candidate(clock, tenant)
    with pytest.raises(HumanOnlyViolation):
        verify_claim(claim, HumanActor(id=human.id, tenant_id=tenant, session_id="stolen"), verifier=verifier,
                     clock=clock)
    with pytest.raises(HumanOnlyViolation):
        verify_claim(claim, HumanActor(id=human.id, tenant_id=new_id(clock), session_id="live"),
                     verifier=verifier, clock=clock)


def test_human_verifies_with_name_and_event(clock, tenant, human, verifier):
    claim, _ = candidate(clock, tenant)
    verified, event = verify_claim(claim, human, verifier=verifier, clock=clock)
    assert verified.state is ClaimState.VERIFIED and verified.verified_by == human.id
    assert verified.verified_at == clock.now() and verified.expires_at is None
    assert event.name is EventName.CLAIM_VERIFIED and event.actor.id == human.id


def test_claim_without_source_or_evidence_cannot_be_verified(clock, tenant, human, verifier):
    claim, _ = candidate(clock, tenant, evidence=None)
    with pytest.raises(ClaimError, match="nothing to verify"):
        verify_claim(claim, human, verifier=verifier, clock=clock)


def test_prices_and_offers_expire_in_thirty_days(clock, tenant, human, verifier):
    price, _ = candidate(clock, tenant, ClaimKind.PRICE, "9.99", "EUR")
    clock.advance(DAY * 3)
    verified, _ = verify_claim(price, human, verifier=verifier, clock=clock)
    assert verified.expires_at == clock.now() + 30 * DAY
    offer, _ = candidate(clock, tenant, ClaimKind.OFFER, "Free first service", None)
    verified, _ = verify_claim(offer, human, verifier=verifier, clock=clock)
    assert verified.expires_at == clock.now() + 30 * DAY


def test_human_set_price_or_offer_expiry_is_capped_at_ninety_days(clock, tenant, human, verifier):
    for kind, value, unit in ((ClaimKind.PRICE, "9.99", "EUR"), (ClaimKind.OFFER, "Free service", None)):
        claim, _ = candidate(clock, tenant, kind, value, unit)
        ok, _ = verify_claim(claim, human, verifier=verifier, clock=clock, expires_at=clock.now() + 90 * DAY)
        assert ok.expires_at == clock.now() + 90 * DAY
        with pytest.raises(ClaimError, match="at most 90 days"):
            verify_claim(claim, human, verifier=verifier, clock=clock, expires_at=clock.now() + 91 * DAY)
    number, _ = candidate(clock, tenant)
    long, _ = verify_claim(number, human, verifier=verifier, clock=clock, expires_at=clock.now() + 3650 * DAY)
    assert long.expires_at == clock.now() + 3650 * DAY


def test_reverification_never_extends_a_shorter_expiry(clock, tenant, human, verifier):
    price, _ = candidate(clock, tenant, ClaimKind.PRICE, "9.99", "EUR", expires_at=clock.now() + 5 * DAY)
    verified, _ = verify_claim(price, human, verifier=verifier, clock=clock)
    assert verified.expires_at == clock.now() + 5 * DAY
    longer, _ = verify_claim(price, human, verifier=verifier, clock=clock, expires_at=clock.now() + 60 * DAY)
    assert longer.expires_at == clock.now() + 60 * DAY
    with pytest.raises(ClaimError):
        verify_claim(price, human, verifier=verifier, clock=clock, expires_at=clock.now())


def test_expiry_and_reverification(clock, tenant, human, verifier):
    number, _ = candidate(clock, tenant, expires_at=clock.now() + 2 * DAY)
    number, _ = verify_claim(number, human, verifier=verifier, clock=clock)
    assert expire_due([number], clock) == []
    clock.advance(2 * DAY)
    [(expired, event)] = expire_due([number], clock)
    assert expired.state is ClaimState.EXPIRED and event.name is EventName.CLAIM_EXPIRED
    with pytest.raises(ClaimError, match="new expiry"):
        verify_claim(expired, human, verifier=verifier, clock=clock)
    again, _ = verify_claim(expired, human, verifier=verifier, clock=clock, expires_at=clock.now() + DAY)
    assert again.state is ClaimState.VERIFIED


