"""WP-28 on Postgres: the tables refuse material without consent, round trip, tenant isolation, no delete."""
# ruff: noqa: F811  (store fixtures imported from wp01)
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest

from tests.wp01_store.conftest import URL, admin, clock, database, make_tenant, urls  # noqa: F401
from tests.wp28_proof.conftest import TRANSCRIPT
from thallo.contracts.actors import HumanActor
from thallo.contracts.enums import ClaimKind, Language, OutcomeKind, SourceKind, UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Claim, Deal, Identity, Lead, Outcome, Person, Source
from thallo.contracts.states import ClaimState, LeadState
from thallo.extensions.proof import Proof, ProofMaterial, Use, brief_notes, extract_notes

pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")
WORDS = "The new scooter starts every morning without a fuss."


class Verifier:
    def is_live_human_session(self, actor):  # noqa: ANN001, ANN201
        return True


def setup(database, tenant, admin):
    clock = database.clock
    ident = Identity(id=new_id(clock), created_at=clock.now(), email=f"{new_id(clock)}@example.invalid")
    admin.execute("INSERT INTO identity (id, created_at, email) VALUES (%s, %s, %s)",
                  (ident.id, ident.created_at, ident.email))
    with database.tenant(tenant.id) as tx:
        now = clock.now()
        user = tx.insert(AppUser(id=new_id(clock), created_at=now, tenant_id=tenant.id, identity_id=ident.id,
                                 display_name="Seller", role=UserRole.SELLER))
        person = tx.insert(Person(id=new_id(clock), created_at=now, tenant_id=tenant.id, display_name="Marco Rossi"))
        lead = tx.insert(Lead(id=new_id(clock), created_at=now, tenant_id=tenant.id, person_id=person.id,
                              state=LeadState.WON, clock_started_at=now))
        deal = tx.insert(Deal(id=new_id(clock), created_at=now, tenant_id=tenant.id, lead_id=lead.id,
                              value=Decimal("100")))
        tx.insert(Outcome(id=new_id(clock), created_at=now, tenant_id=tenant.id, deal_id=deal.id,
                          kind=OutcomeKind.WON, amount=Decimal("100"), recorded_by=user.id, occurred_at=now))
        src = tx.insert(Source(id=new_id(clock), created_at=now, tenant_id=tenant.id, kind=SourceKind.VOICE_ANSWER,
                               language=Language.EN, title="Answer", transcript=TRANSCRIPT, untrusted=True,
                               captured_at=now))
        claim = tx.insert(Claim(id=new_id(clock), created_at=now, tenant_id=tenant.id, code="C-0101",
                                kind=ClaimKind.NUMBER, value="30", unit="percent", state=ClaimState.VERIFIED,
                                source_id=src.id, verified_by=user.id, verified_at=now,
                                expires_at=now + timedelta(days=60)))
    return user, person, deal, src, claim


def test_round_trip_withdrawal_isolation_and_refusals(database, admin):  # noqa: F811
    tenant = make_tenant(database, "Proof")
    user, person, deal, src, claim = setup(database, tenant, admin)
    human = HumanActor(id=user.id, tenant_id=tenant.id, session_id="live")
    s = TRANSCRIPT.index(WORDS)
    with database.tenant(tenant.id) as tx:
        p = Proof(tx, tenant.id, database.clock)
        g = p.record_consent(deal.id, uses=[Use.QUOTE, Use.RESULT], source_ids=[src.id], evidence="yes on a call",
                             actor=human, verifier=Verifier())
        q = p.add_quote(g.id, source_id=src.id, start=s, end=s + len(WORDS))
        r = p.add_result(g.id, claim_id=claim.id)
        story = p.case_story([q.id, r.id])
        assert "{{C-0101}}" in story.body
        assert extract_notes(p, g.id)
        assert brief_notes(tx, database.clock.now())
    database.clock.advance(seconds=2)
    with database.tenant(tenant.id) as tx:
        p = Proof(tx, tenant.id, database.clock)
        counts = p.withdraw(g.consent_id, actor=human, verifier=Verifier())
        assert counts["material"] == 2 and counts["pieces"] == 1
        assert brief_notes(tx, database.clock.now()) == []
    other = make_tenant(database, "Other proof")
    with database.tenant(other.id) as tx:
        assert tx.list_all(ProofMaterial) == []
    with pytest.raises(psycopg.errors.NotNullViolation), database.tenant(tenant.id) as tx:
        tx.execute("INSERT INTO proof_material (id, created_at, tenant_id, kind, grant_id, consent_id, person_id,"
                   " deal_id, claim_id, state) VALUES (gen_random_uuid(), now(), %s, 'result', %s, NULL, %s, %s,"
                   " %s, 'live')", (tenant.id, g.id, person.id, deal.id, claim.id))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("DELETE FROM proof_material")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("UPDATE proof_grant SET name_shown = 'x'")
