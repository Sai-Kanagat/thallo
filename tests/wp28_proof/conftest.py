"""WP-28 tests reuse the lead desk world, plus one won customer with a recorded answer and a verified result."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.wp13_leads.conftest import World, world  # noqa: F401
from tests.wp13_leads.test_desk import _consented_lead
from thallo.contracts.enums import ClaimKind, Language, SourceKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import Claim, Deal, Lead, Source
from thallo.contracts.states import ClaimState, LeadState
from thallo.extensions.proof import Proof

TRANSCRIPT = ("They called me back within the hour and explained everything. "
              "The new scooter starts every morning without a fuss. "
              "We saved 30 percent on the service. "
              "Marco at the shop was really patient with me.")


@dataclass
class Won:
    lead_id: str
    person_id: str
    deal_id: str
    source_id: str
    claim_id: str


def _to_held(w, tx, lead_id):  # noqa: ANN001
    """Stand in for the reply the seller sent: the lead is contacted, so an outcome can be recorded."""
    tx.update(Lead, lead_id, state=LeadState.CONTACTED)


def make_won(w, tx) -> Won:  # noqa: ANN001
    lead = _consented_lead(w, tx)
    _to_held(w, tx, lead.id)
    w.desk(tx).outcome(lead.id, "won", actor=w.human(w.seller), verifier=w.verifier, amount=Decimal("2400"))
    deal = tx.list_all(Deal, lead_id=lead.id)[0]
    now = w.clock.now()
    src = tx.insert(Source(id=new_id(w.clock), created_at=now, tenant_id=w.tenant.id, kind=SourceKind.VOICE_ANSWER,
                           language=Language.EN, title="Customer answer", transcript=TRANSCRIPT, untrusted=True,
                           captured_at=now))
    claim = tx.insert(Claim(id=new_id(w.clock), created_at=now, tenant_id=w.tenant.id, code="C-0101",
                            kind=ClaimKind.NUMBER, value="30", unit="percent", state=ClaimState.VERIFIED,
                            source_id=src.id, verified_by=w.founder.id, verified_at=now,
                            expires_at=now + timedelta(days=60)))
    return Won(lead.id, lead.person_id, deal.id, src.id, claim.id)


@pytest.fixture
def won(world):  # noqa: F811
    with world.backend.tenant(world.tenant.id) as tx:
        return make_won(world, tx)


def proof(w, tx) -> Proof:  # noqa: ANN001
    return Proof(tx, w.tenant.id, w.clock)
