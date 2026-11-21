"""WP-28 in memory: consent before drafting, exact words, verified results, asks through the desk,
withdrawal for pending and live pieces, buyer-language notes."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.wp13_leads.conftest import FakeSender
from tests.wp28_proof.conftest import TRANSCRIPT, _to_held, proof
from thallo.contracts.actors import AgentActor, HumanOnlyViolation
from thallo.contracts.enums import Channel, ClaimKind, ConsentScope, Language, LossReason, MediaKind, MediaProvenance
from thallo.contracts.ids import new_id
from thallo.contracts.models import Claim, Consent, Media, Message, Publication, Variant
from thallo.contracts.states import ClaimState
from thallo.extensions.proof import AskKind, ProofAsk, ProofError, ProofFlag, Use, brief_notes, extract_notes
from thallo.leads import body_hash

ROOT = Path(__file__).resolve().parents[2]
WORDS = "The new scooter starts every morning without a fuss."


def span(words=WORDS):
    s = TRANSCRIPT.index(words)
    return s, s + len(words)


def grant(w, tx, won, uses=(Use.QUOTE, Use.NAME, Use.RESULT), name="Marco R., Bologna"):
    return proof(w, tx).record_consent(won.deal_id, uses=uses, name_shown=name, source_ids=[won.source_id],
                                       evidence="Said yes on a call on 3 March, wording proof-v1",
                                       actor=w.human(w.seller), verifier=w.verifier)


def variant(w, tx):
    return tx.insert(Variant(id=new_id(w.clock), created_at=w.clock.now(), tenant_id=w.tenant.id,
                             bundle_id=new_id(w.clock), channel=Channel.INSTAGRAM, language=Language.EN, text="x",
                             task_type="post"))


# ---------------------------------------------------------------- the consent guard

def test_nothing_is_drafted_without_a_consent(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        with pytest.raises(ProofError) as e:
            p.add_quote(new_id(world.clock), source_id=won.source_id, start=0, end=20)
        assert e.value.code == "no_consent"


def test_consent_is_human_only_names_the_customer_and_needs_evidence(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        with pytest.raises(HumanOnlyViolation):
            p.record_consent(won.deal_id, uses=[Use.QUOTE], source_ids=[won.source_id], evidence="yes",
                             actor=AgentActor(id="agent"), verifier=world.verifier)
        with pytest.raises(ProofError):
            p.record_consent(won.deal_id, uses=[Use.QUOTE], source_ids=[won.source_id], evidence="  ",
                             actor=world.human(world.seller), verifier=world.verifier)
        with pytest.raises(ProofError):  # a name use needs the exact name text
            p.record_consent(won.deal_id, uses=[Use.NAME], evidence="yes on a call",
                             actor=world.human(world.seller), verifier=world.verifier)
        g = grant(world, tx, won)
        c = tx.get(Consent, g.consent_id)
        assert c.person_id == won.person_id and c.scopes == {ConsentScope.QUOTE}
        assert "Said yes on a call" in c.evidence


def test_only_a_won_deal_gives_proof(world):
    from tests.wp13_leads.test_desk import _consented_lead
    from thallo.contracts.models import Deal
    with world.backend.tenant(world.tenant.id) as tx:
        lead = _consented_lead(world, tx)
        _to_held(world, tx, lead.id)
        world.desk(tx).outcome(lead.id, "lost", actor=world.human(world.seller), verifier=world.verifier,
                               reason=LossReason.PRICE)
        deal = tx.list_all(Deal, lead_id=lead.id)[0]
        with pytest.raises(ProofError) as e:
            proof(world, tx).record_consent(deal.id, uses=[Use.QUOTE], evidence="yes", source_ids=[],
                                            actor=world.human(world.seller), verifier=world.verifier)
        assert e.value.code == "not_won"


def test_scope_matrix(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won, uses=(Use.QUOTE,), name=None)
        s, e = span()
        q = p.add_quote(g.id, source_id=won.source_id, start=s, end=e)
        with pytest.raises(ProofError) as err:
            p.add_result(g.id, claim_id=won.claim_id)
        assert err.value.code == "consent_scope"
        media = tx.insert(Media(id=new_id(world.clock), created_at=world.clock.now(), tenant_id=world.tenant.id,
                                kind=MediaKind.IMAGE, sha256="a" * 64, provenance=MediaProvenance.CLIENT_PHOTO,
                                mime="image/jpeg", size_bytes=10, storage_uri="x"))
        with pytest.raises(ProofError) as err:
            p.add_photo(g.id, media_id=media.id)
        assert err.value.code == "consent_scope"
        card = p.quote_card(q.id)
        assert card.card["attribution"] == "A customer"  # no name use: anonymous
        g2 = grant(world, tx, won, uses=(Use.QUOTE, Use.PHOTO), name=None)
        assert tx.get(Consent, g2.consent_id).scopes == {ConsentScope.QUOTE, ConsentScope.LIKENESS}
        ph = p.add_photo(g2.id, media_id=media.id)
        q2 = p.add_quote(g2.id, source_id=won.source_id, start=s, end=e)
        with pytest.raises(ProofError) as err:  # one piece, one consent
            p.quote_card(q.id, photo_id=ph.id)
        assert err.value.code == "mixed_consent"
        assert p.quote_card(q2.id, photo_id=ph.id).card["photo_media_id"] == media.id


# ---------------------------------------------------------------- exact words, verified results

def test_quote_card_uses_exact_words_and_carries_the_scope(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won)
        s, e = span()
        card = p.quote_card(p.add_quote(g.id, source_id=won.source_id, start=s, end=e).id)
        assert card.card["quote"] == WORDS == TRANSCRIPT[s:e]
        assert card.card["attribution"] == "Marco R., Bologna"
        assert card.card["consent_scope"] == ["quote"] and card.card["uses"] == ["name", "quote", "result"]
        assert card.consent_id == g.consent_id and WORDS in card.body


def test_quotes_refuse_bad_spans_numbers_and_unnamed_recordings(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won)
        s, e = span("We saved 30 percent on the service.")
        with pytest.raises(ProofError) as err:
            p.add_quote(g.id, source_id=won.source_id, start=s, end=e)
        assert err.value.code == "number_in_quote"
        with pytest.raises(ProofError):
            p.add_quote(g.id, source_id=won.source_id, start=0, end=len(TRANSCRIPT) + 5)
        with pytest.raises(ProofError):
            p.add_quote(g.id, source_id=won.source_id, start=s - 1, end=s + 10)  # edge space
        with pytest.raises(ProofError) as err:
            p.add_quote(g.id, source_id=new_id(world.clock), start=0, end=20)
        assert err.value.code == "bad_source"


def test_an_edited_transcript_stops_the_quote(world, won):
    from thallo.contracts.models import Source
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won)
        s, e = span()
        q = p.add_quote(g.id, source_id=won.source_id, start=s, end=e)
        tx.update(Source, won.source_id, transcript=TRANSCRIPT.replace("without a fuss", "like magic!!"))
        with pytest.raises(ProofError) as err:
            p.quote_card(q.id)
        assert err.value.code == "quote_changed"


def test_case_story_has_exact_words_and_results_only_as_claim_slots(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won)
        s, e = span()
        q = p.add_quote(g.id, source_id=won.source_id, start=s, end=e)
        r = p.add_result(g.id, claim_id=won.claim_id)
        story = p.case_story([q.id, r.id])
        assert "{{C-0101}}" in story.body and f"“{WORDS}”" in story.body
        assert not any(ch.isdigit() for ch in story.body.replace("{{C-0101}}", ""))
        assert story.claim_ids == [won.claim_id]
        with pytest.raises(ProofError):
            p.case_story([q.id])  # no result: no story


def test_unverified_or_expired_results_are_refused(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won)
        cand = tx.insert(Claim(id=new_id(world.clock), created_at=world.clock.now(), tenant_id=world.tenant.id,
                               code="C-0102", kind=ClaimKind.NUMBER, value="2",
                               unit="hours", state=ClaimState.CANDIDATE))
        with pytest.raises(ProofError) as err:
            p.add_result(g.id, claim_id=cand.id)
        assert err.value.code == "claim_not_verified"
        r = p.add_result(g.id, claim_id=won.claim_id)
        s, e = span()
        q = p.add_quote(g.id, source_id=won.source_id, start=s, end=e)
        world.clock.advance(days=61)  # the claim expires
        with pytest.raises(ProofError) as err:
            p.case_story([q.id, r.id])
        assert err.value.code == "claim_not_verified"


# ---------------------------------------------------------------- asks through the lead desk

def test_review_and_referral_asks_are_desk_drafts_sent_only_by_a_tap(world, won):
    sender = FakeSender()
    with world.backend.tenant(world.tenant.id) as tx:
        desk, p = world.desk(tx), proof(world, tx)
        with pytest.raises(ProofError):
            p.draft_ask(desk, won.lead_id, AskKind.REVIEW, review_link="javascript:alert(1)")
        rev = p.draft_ask(desk, won.lead_id, AskKind.REVIEW, review_link="https://g.page/r/abc/review")
        ref = p.draft_ask(desk, won.lead_id, AskKind.REFERRAL)
        with pytest.raises(ProofError) as err:
            p.draft_ask(desk, won.lead_id, AskKind.REFERRAL)
        assert err.value.code == "already_asked"
        assert {a.message_id for a in tx.list_all(ProofAsk)} == {rev.id, ref.id}
        assert "https://g.page/r/abc/review" in rev.body and sender.requests == []
        with pytest.raises(HumanOnlyViolation):
            desk.approve_and_send(ref.id, actor=AgentActor(id="a"),
                                  verifier=world.verifier, sender=sender, body_sha256=body_hash(ref.body))
        out = desk.approve_and_send(ref.id, actor=world.human(world.seller), verifier=world.verifier,
                                    sender=sender, body_sha256=body_hash(ref.body))
        assert out["status"] == "open_in_app" and len(sender.requests) == 1
        assert sender.requests[0].message_id == ref.id


def test_asks_need_a_won_deal_and_contact_consent(world, won):
    from tests.wp13_leads.test_desk import _event
    with world.backend.tenant(world.tenant.id) as tx:
        desk, p = world.desk(tx), proof(world, tx)
        open_lead = desk.intake(_event(world, phone="+39 333 999 0000", display_name="Anna"))
        with pytest.raises(ProofError) as err:
            p.draft_ask(desk, open_lead.id, AskKind.REFERRAL)
        assert err.value.code == "not_won"
        c = next(x for x in tx.list_all(Consent, person_id=won.person_id) if ConsentScope.CONTACT in x.scopes)
        tx.update(Consent, c.id, withdrawn_at=world.clock.now())
        with pytest.raises(ProofError) as err:
            p.draft_ask(desk, won.lead_id, AskKind.REFERRAL)
        assert err.value.code == "no_consent"
        assert not tx.list_all(Message, lead_id=won.lead_id, kind="reply_draft")


def test_proof_never_builds_a_send_request():
    src = (ROOT / "thallo/extensions/proof").glob("*.py")
    for f in src:
        tree = ast.parse(f.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"MessageSendRequest", "approve_and_send", "run", "send"}, f.name


# ---------------------------------------------------------------- withdrawal

def test_withdrawal_stops_future_pieces_and_flags_placed_ones(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won)
        s, e = span()
        q = p.add_quote(g.id, source_id=won.source_id, start=s, end=e)
        pending, live, unplaced = p.quote_card(q.id), p.quote_card(q.id), p.quote_card(q.id)
        v1, v2 = variant(world, tx), variant(world, tx)
        p.place(pending.id, v1.id)
        p.place(live.id, v2.id)
        tx.insert(Publication(id=new_id(world.clock), created_at=world.clock.now(), tenant_id=world.tenant.id,
                              variant_id=v2.id, approval_id=new_id(world.clock), idempotency_key="k",
                              content_sha256="b" * 64))
        extract_notes(p, g.id)
        assert brief_notes(tx, world.clock.now())
        world.clock.advance(seconds=5)
        with pytest.raises(HumanOnlyViolation):
            p.withdraw(g.consent_id, actor=AgentActor(id="a"), verifier=world.verifier)
        counts = p.withdraw(g.consent_id, actor=world.human(world.seller), verifier=world.verifier)
        assert counts["pieces"] == 3 and counts["flags"] == 2 and counts["live_flags"] == 1
        flags = {f.piece_id: f.live for f in tx.list_all(ProofFlag)}
        assert flags == {pending.id: False, live.id: True} and unplaced.id not in flags
        assert p.sweep()["flags"] == 0  # idempotent
        for call in (lambda: p.quote_card(q.id), lambda: p.place(unplaced.id, v1.id),
                     lambda: p.add_quote(g.id, source_id=won.source_id, start=s, end=e)):
            with pytest.raises(ProofError):
                call()
        assert brief_notes(tx, world.clock.now()) == []
        assert tx.get(Variant, v2.id) is not None  # nothing unpublished by the machine
        f = next(f for f in p.open_flags() if f.live)
        p.resolve_flag(f.id, actor=world.human(world.founder), verifier=world.verifier)
        assert len(p.open_flags()) == 1


def test_sweep_catches_a_withdrawal_written_elsewhere(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won)
        s, e = span()
        q = p.add_quote(g.id, source_id=won.source_id, start=s, end=e)
        tx.update(Consent, g.consent_id, withdrawn_at=world.clock.now())
        assert p.sweep()["material"] == 1
        with pytest.raises(ProofError):
            p.quote_card(q.id)


# ---------------------------------------------------------------- buyer language

def test_buyer_notes_are_exact_phrases_without_numbers_or_names(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won)
        notes = extract_notes(p, g.id)
        assert extract_notes(p, g.id) == []  # rerun adds nothing
        phrases = [n.phrase for n in notes]
        assert WORDS.rstrip(".") in phrases
        assert not any(ch.isdigit() for ph in phrases for ch in ph)
        assert not any("Marco" in ph for ph in phrases)
        for n in notes:
            assert TRANSCRIPT[n.span_start:n.span_end] == n.phrase
        assert set(brief_notes(tx, world.clock.now())) == set(phrases)


def test_buyer_notes_need_a_quote_consent(world, won):
    with world.backend.tenant(world.tenant.id) as tx:
        p = proof(world, tx)
        g = grant(world, tx, won, uses=(Use.RESULT,), name=None)
        with pytest.raises(ProofError):
            extract_notes(p, g.id)

