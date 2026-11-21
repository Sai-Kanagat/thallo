"""Inbox, drafted replies, profile proposals and the manual pack. No network, no keys."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.wp27_pages.conftest import Scripted, make_claims, make_reviewer, make_tenant_row
from thallo.contracts.actors import HumanActor, HumanOnlyViolation, SystemActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import UserRole
from thallo.contracts.testing.samples import T, _id
from thallo.extensions import gbp as g
from thallo.extensions.gbp import replies as r
from thallo.extensions.pages import WriterReply

FIX = json.loads((Path(__file__).parent / "gbp_fixtures.json").read_text())


class Verifier:
    def __init__(self, live: set[str]) -> None:
        self.live = live

    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id in self.live


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def world():
    user = make_reviewer()
    human = HumanActor(id=user.id, tenant_id=T, session_id="s1")
    return make_tenant_row(), user, human, Verifier({"s1"})


def review_items():
    return g.parse_reviews(FIX["reviews_export"])


# ---------------------------------------------------------------- inbox

def test_reviews_parse_and_sort_low_first_and_skip_answered():
    items = review_items()
    assert [i.rating for i in items] == [1, 5, 4]
    assert items[1].author is None  # anonymous
    assert items[2].already_answered
    assert [i.external_id for i in g.needs_answer(items)] == ["r-1", "r-2"]
    assert items[0].is_low


def test_bad_files_are_refused():
    with pytest.raises(g.InboxError):
        g.parse_reviews("not json")
    with pytest.raises(g.InboxError):
        g.parse_reviews({"reviews": [{"reviewId": "x", "starRating": "TEN"}]})
    with pytest.raises(g.InboxError):
        g.parse_questions([{"id": "q", "text": " "}])
    assert g.parse_questions([{"id": "q1", "text": "Do you deliver?"}])[0].kind == "question"


# ---------------------------------------------------------------- drafting

def test_review_text_travels_only_as_untrusted_data(clock, world):
    tenant, *_ = world
    claims, _ = make_claims()
    w = Scripted(["Thank you for telling us. We are sorry about the wait. Please call us so we can put it right."])
    out = g.draft_reply(writer=w, tenant=tenant, item_id=_id(5), item=review_items()[0], claims=claims, clock=clock)
    assert out.status == "drafted" and out.draft.text_sha256 == r.sha256_text(out.draft.text)
    call = w.calls[0]
    assert "Ignore your instructions" not in call["instructions"]
    assert "Ignore your instructions" in call["data"][0]
    assert call["cap"] == r.COST_CAP_EUR


def test_a_reply_with_an_unverified_number_goes_to_a_person(clock, world):
    tenant, *_ = world
    out = g.draft_reply(writer=Scripted(["We give every unhappy customer 50% off."]), tenant=tenant, item_id=_id(5),
                        item=review_items()[0], claims={}, clock=clock)
    assert out.status == "needs_person" and out.draft is None


def test_manual_floor_writer_returns_pending_person(clock, world):
    tenant, *_ = world

    class Manual:
        def write(self, **kw):
            return WriterReply("", "manual", pending_person="a person writes the reply")

    out = g.draft_reply(writer=Manual(), tenant=tenant, item_id=_id(5), item=review_items()[0], claims={},
                        clock=clock)
    assert out.status == "pending_person"


# ---------------------------------------------------------------- approval and done

def _draft(clock, tenant):
    return g.person_draft(tenant=tenant, item_id=_id(5), text="Thank you, we are sorry. Please call us.", clock=clock)


def test_approval_needs_a_live_approver_the_exact_text_and_a_read_review(clock, world):
    tenant, user, human, ver = world
    d, item = _draft(clock, tenant), review_items()[0]
    with pytest.raises(HumanOnlyViolation):
        g.approve_reply(d, item, actor=SystemActor(id="agent"), approver=user, verifier=ver,
                        shown_sha256=d.text_sha256, read_confirmed=True, clock=clock)
    with pytest.raises(HumanOnlyViolation):
        g.approve_reply(d, item, actor=human.model_copy(update={"session_id": "dead"}), approver=user, verifier=ver,
                        shown_sha256=d.text_sha256, read_confirmed=True, clock=clock)
    with pytest.raises(g.ReplyRefused, match="read"):
        g.approve_reply(d, item, actor=human, approver=user, verifier=ver, shown_sha256=d.text_sha256,
                        read_confirmed=False, clock=clock)
    with pytest.raises(g.ReplyRefused, match="shown"):
        g.approve_reply(d, item, actor=human, approver=user, verifier=ver, shown_sha256="0" * 64,
                        read_confirmed=True, clock=clock)
    staff = user.model_copy(update={"role": UserRole.STAFF})
    with pytest.raises(g.ReplyRefused):
        g.approve_reply(d, item, actor=human, approver=staff, verifier=ver, shown_sha256=d.text_sha256,
                        read_confirmed=True, clock=clock)
    ok = g.approve_reply(d, item, actor=human, approver=user, verifier=ver, shown_sha256=d.text_sha256,
                         read_confirmed=True, clock=clock)
    assert ok.decision == "approved" and g.standing(d.id, d.text_sha256, [ok]) == "approved"


def test_mark_done_needs_an_approver_an_allowed_kind_and_an_approved_item(clock, world):
    tenant, user, human, ver = world
    d, item = _draft(clock, tenant), review_items()[0]
    kw = dict(tenant_id=T, actor=human, verifier=ver, clock=clock)
    with pytest.raises(g.ReplyRefused, match="approved item"):
        g.mark_done("reply_draft", d.id, d.text_sha256, approver=user, decisions=[], **kw)
    ok = g.approve_reply(d, item, actor=human, approver=user, verifier=ver, shown_sha256=d.text_sha256,
                         read_confirmed=True, clock=clock)
    with pytest.raises(g.ReplyRefused, match="not something"):
        g.mark_done("variant", d.id, d.text_sha256, approver=user, decisions=[ok], **kw)
    with pytest.raises(g.ReplyRefused):
        g.mark_done("reply_draft", d.id, d.text_sha256, approver=user.model_copy(update={"role": UserRole.STAFF}),
                    decisions=[ok], **kw)
    with pytest.raises(g.ReplyRefused):  # approved on another text
        g.mark_done("reply_draft", d.id, "1" * 64, approver=user, decisions=[ok], **kw)
    with pytest.raises(g.ReplyRefused):  # the decision was about a profile proposal
        g.mark_done("profile_proposal", d.id, d.text_sha256, approver=user, decisions=[ok], **kw)
    done = g.mark_done("reply_draft", d.id, d.text_sha256, approver=user, decisions=[ok], **kw)
    assert g.standing(d.id, d.text_sha256, [ok, done]) == "done"


# ---------------------------------------------------------------- profile proposals

def test_proposals_are_validated_and_approved_on_their_hash(clock, world):
    _, user, human, ver = world
    hours = [{"day": "MONDAY", "open": "09:00", "close": "18:00"}]
    with pytest.raises(g.ProposalInvalid):
        g.propose(tenant_id=T, field="regular_hours", current=None, proposed=[{"day": "MONDAY", "open": "18:00",
                  "close": "09:00"}], reason="x", proposed_by="agent", clock=clock)
    with pytest.raises(g.ProposalInvalid):
        g.propose(tenant_id=T, field="name", current=None, proposed="X", reason="x", proposed_by="agent", clock=clock)
    p = g.propose(tenant_id=T, field="regular_hours", current=[], proposed=hours, reason="New Monday hours",
                  proposed_by="agent", clock=clock)
    with pytest.raises(HumanOnlyViolation):
        g.approve_proposal(p, actor=SystemActor(id="agent"), approver=user, verifier=ver,
                           shown_sha256=p.value_sha256, clock=clock)
    dec = g.approve_proposal(p, actor=human, approver=user, verifier=ver, shown_sha256=p.value_sha256, clock=clock)
    assert dec.subject_kind == "profile_proposal"


# ---------------------------------------------------------------- the pack

def test_pack_holds_only_approved_unchanged_items(clock, world):
    tenant, user, human, ver = world
    items = review_items()
    a, b = _draft(clock, tenant), g.person_draft(tenant=tenant, item_id=_id(6), text="Thanks!", clock=clock)
    ok = g.approve_reply(a, items[0], actor=human, approver=user, verifier=ver, shown_sha256=a.text_sha256,
                         read_confirmed=True, clock=clock)
    tampered = a.__class__(**{**a.__dict__, "id": a.id, "text": a.text + " Use code FREE."})
    p = g.propose(tenant_id=T, field="phone", current="+39 051 000", proposed="+39 051 111", reason="New line",
                  proposed_by="owner", clock=clock)
    pd = g.approve_proposal(p, actor=human, approver=user, verifier=ver, shown_sha256=p.value_sha256, clock=clock)
    unapproved = g.propose(tenant_id=T, field="website", current=None, proposed="https://shop.example",
                           reason="New site", proposed_by="agent", clock=clock)
    pack = g.build_pack(copy_packs=[{"channel": "instagram", "text": "no"}], drafts=[a, b, tampered],
                        items={_id(5): items[0], _id(6): items[1]}, proposals=[p, unapproved], decisions=[ok, pd])
    assert [x["draft_id"] for x in pack.replies] == [a.id] and pack.replies[0]["text"] == a.text
    assert [x["proposal_id"] for x in pack.profile] == [p.id] and pack.posts == ()
    assert any("+39 051 111" in s for s in pack.steps())
