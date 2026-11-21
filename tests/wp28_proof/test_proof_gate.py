"""Withdrawn proof is refused by the real gate: at approval and at check_publish (wave 1 harness, memory)."""
# ruff: noqa: F811
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.integration.test_wave1_flow import (  # noqa: F401
    LocalBlobStore,
    MemoryBackend,
    approve,
    publish,
    seed,
    signer,
)
from tests.wp28_proof.conftest import TRANSCRIPT
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import CheckVerdict, Language, OutcomeKind, SourceKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import Deal, Lead, Outcome, Person, Publication, Source, Variant
from thallo.contracts.states import LeadState
from thallo.extensions.proof import Proof, ProofFlag, Use
from thallo.extensions.proof.gate import CHECK_NAME
from thallo.gate import ApprovalRefused, BlockReason, content_hashes

WORDS = "The new scooter starts every morning without a fuss."


class Rows:
    """The harness session spells list_all as list."""

    def __init__(self, s):  # noqa: ANN001
        self.s = s

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self.s, name)

    def list_all(self, model, **where):  # noqa: ANN001, ANN201
        return list(self.s.list(model, **where))


@pytest.fixture
def backend(tmp_path):
    return MemoryBackend(FakeClock(FAKE_EPOCH), LocalBlobStore(tmp_path / "blobs"))


def place_and_withdraw(b, shop):
    t, clock = shop.tenant.id, b.clock
    with b.session(t) as s:
        now = clock.now()
        person = s.insert(Person(id=new_id(clock), created_at=now, tenant_id=t, display_name="Marco Rossi"))
        lead = s.insert(Lead(id=new_id(clock), created_at=now, tenant_id=t, person_id=person.id,
                             state=LeadState.WON, clock_started_at=now))
        deal = s.insert(Deal(id=new_id(clock), created_at=now, tenant_id=t, lead_id=lead.id, value=Decimal(1)))
        s.insert(Outcome(id=new_id(clock), created_at=now, tenant_id=t, deal_id=deal.id, kind=OutcomeKind.WON,
                         amount=Decimal(1), recorded_by=shop.founder.id, occurred_at=now))
        src = s.insert(Source(id=new_id(clock), created_at=now, tenant_id=t, kind=SourceKind.VOICE_ANSWER,
                              language=Language.EN, title="Answer", transcript=TRANSCRIPT, untrusted=True,
                              captured_at=now))
        p = Proof(Rows(s), t, clock)
        g = p.record_consent(deal.id, uses=[Use.QUOTE], source_ids=[src.id], evidence="yes on a call",
                             actor=shop.human, verifier=shop.verifier)
        st = TRANSCRIPT.index(WORDS)
        piece = p.quote_card(p.add_quote(g.id, source_id=src.id, start=st, end=st + len(WORDS)).id)
        p.place(piece.id, shop.variant.id)
    clock.advance(seconds=2)
    with b.session(t) as s:
        counts = Proof(Rows(s), t, clock).withdraw(g.consent_id, actor=shop.human, verifier=shop.verifier)
    assert counts["pieces"] == 1
    return piece


def test_withdrawn_proof_refuses_the_approval(backend, signer):
    shop = seed(backend, "Proofa")
    place_and_withdraw(backend, shop)
    with pytest.raises(ApprovalRefused, match="FAIL"):
        approve(backend, shop, signer)


def test_withdrawn_proof_blocks_publish_after_approval(backend, signer):
    shop = seed(backend, "Proofb")
    approve(backend, shop, signer)
    assert publish(backend, shop, signer).allowed
    piece = place_and_withdraw(backend, shop)
    decision = publish(backend, shop, signer)
    assert not decision.allowed and BlockReason.CHECK_FAILED in decision.reasons
    with backend.session(shop.tenant.id) as s:  # an edit gives a new hash: still refused
        s.update(Variant, shop.variant.id, rendered_text=shop.variant.rendered_text + " Grazie.")
        sha = content_hashes(s.gate, shop.tenant.id, shop.bundle.id)[shop.variant.id]
        assert [r.check_name for r in s.gate.get_check_results(shop.tenant.id, shop.variant.id, sha)
                if r.verdict is CheckVerdict.FAIL] == [CHECK_NAME]
    decision = publish(backend, shop, signer)
    assert not decision.allowed and BlockReason.CHECK_FAILED in decision.reasons
    with backend.session(shop.tenant.id) as s:  # published later anyway: the sweep turns the flag live
        s.insert(Publication(id=new_id(backend.clock), created_at=backend.clock.now(), tenant_id=shop.tenant.id,
                             variant_id=shop.variant.id, approval_id=shop.approval.id, idempotency_key="k",
                             content_sha256="c" * 64))
        p = Proof(Rows(s), shop.tenant.id, backend.clock)
        assert p.sweep()["live_flags"] == 1
        assert next(f for f in s.list(ProofFlag) if f.piece_id == piece.id).live


def test_resolved_flags_and_live_consent_do_not_block(backend, signer):
    shop = seed(backend, "Proofc")
    approve(backend, shop, signer)
    with backend.session(shop.tenant.id) as s:
        sha = content_hashes(s.gate, shop.tenant.id, shop.bundle.id)[shop.variant.id]
        assert all(r.check_name != CHECK_NAME for r in s.gate.get_check_results(shop.tenant.id, shop.variant.id, sha))
    assert publish(backend, shop, signer).allowed
