from __future__ import annotations

from datetime import timedelta

import pytest

from thallo.contracts.actors import AgentActor, HumanActor, HumanOnlyViolation
from thallo.contracts.enums import ApprovalSubject, Channel, EditKind, RuleScope, TextRole, UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.states import IllegalTransition, RuleProposalState
from thallo.learning.blind import BlindError, BlindSession, Dimension, Piece
from thallo.learning.diffs import classify
from thallo.learning.evals import Change, ModelGrader, case_set_sha, run_eval
from thallo.learning.golden import GOLDEN_DIR, load_golden
from thallo.learning.lifecycle import (
    EXPIRY,
    LifecycleError,
    accept,
    expire_due,
    live_rules,
    promote,
    record_use,
    reject,
    transfer_test,
)
from thallo.learning.prompts import ChangeBlocked, PromptRegistry, gate
from thallo.learning.proposals import EditContext, propose
from thallo.learning.records import BlindSource, CaseKind, ChangeKind, RegressionCase
from thallo.learning.rules import AvoidPhrase, MaxExclamations, PreferWording, parse_rule
from thallo.learning.summary import ApprovalWithOutcomes, PieceOutcome

from .conftest import T1, T2, USER, make_edit, make_user

# ---------------------------------------------------------------- classifier


@pytest.mark.parametrize(("draft", "final", "kind"), [
    ("A loaf is {{C-0001}} today.", "A loaf is {{C-0002}} today.", EditKind.FACT_FIX),
    ("We open at 8 today.", "We open at 9 today.", EditKind.FACT_FIX),
    ("Our sourdugh is baked daily.", "Our sourdough is baked daily.", EditKind.TYPO),
    ("Our bread is baked daily", "Our bread is baked daily.", EditKind.TYPO),
    ("Truly our bread is baked daily.", "Our bread is baked daily.", EditKind.CUT),
    ("Our bread is amazing and baked daily.", "Our bread is good and baked daily.", EditKind.WORD_SWAP),
    ("We bake bread. We sell cakes.", "We sell cakes. We bake bread.", EditKind.STRUCTURE_CHANGE),
    ("We bake bread and also cakes.", "We bake bread.\n\nAnd cakes.", EditKind.STRUCTURE_CHANGE),
    ("Our bread is baked daily with care.", "Every morning, the ovens go on before anyone else wakes.",
     EditKind.TONE_SHIFT),
])
def test_classify(draft, final, kind):
    assert classify(draft, final).kind is kind


def test_two_real_word_changes_are_not_a_typo():
    assert classify("Our great bread is baked.", "Our good loaf is baked.").kind is not EditKind.TYPO


# ---------------------------------------------------------------- rules


def test_typed_rules_round_trip():
    for r in (AvoidPhrase(phrase="game-changing"), PreferWording(avoid="amazing", use="good"),
              MaxExclamations(limit=0)):
        assert parse_rule(r.model_dump(mode="json")) == r
    assert AvoidPhrase(phrase="truly").broken_by("Truly good")
    assert not AvoidPhrase(phrase="truly").broken_by("untruly good")
    assert MaxExclamations(limit=0).broken_by("Hi!")


# ---------------------------------------------------------------- proposals


def ctx(clock, before, after, pillar=None, channel=Channel.INSTAGRAM, tenant=T1):
    clock.advance(minutes=1)
    return EditContext(make_edit(clock, before, after, tenant), channel=channel, pillar=pillar)


def test_two_similar_edits_make_one_scoped_proposal(clock):
    edits = [ctx(clock, "Truly our bread is good.", "Our bread is good.", pillar="craft"),
             ctx(clock, "Truly the cakes sell out.", "The cakes sell out.", pillar="craft")]
    rnd = propose(tenant_id=T1, edits=edits, existing_cases=[], known=[], clock=clock)
    assert len(rnd.proposed) == 1
    p = rnd.proposed[0].proposal
    assert parse_rule(p.rule) == AvoidPhrase(phrase="truly")
    assert (p.scope, p.scope_ref) == (RuleScope.PILLAR, "craft")
    assert p.triggering_edit_ids == [e.edit.id for e in edits]
    assert p.regression_case["draft"] == edits[0].edit.before_text
    assert len(rnd.proposed[0].cases) == 2
    assert all(k is EditKind.CUT for k in rnd.kinds.values())


def test_scope_widens_to_channel_then_brand(clock):
    edits = [ctx(clock, "Truly a.", "A.", pillar="x"), ctx(clock, "Truly b.", "B.", pillar="y")]
    p = propose(tenant_id=T1, edits=edits, existing_cases=[], known=[], clock=clock).proposed[0].proposal
    assert (p.scope, p.scope_ref) == (RuleScope.CHANNEL, "instagram")
    edits = [ctx(clock, "Truly a.", "A.", channel=Channel.EMAIL), ctx(clock, "Truly b.", "B.")]
    p = propose(tenant_id=T1, edits=edits, existing_cases=[], known=[], clock=clock).proposed[0].proposal
    assert p.scope is RuleScope.BRAND


def test_one_edit_typos_and_fact_fixes_make_no_rule(clock):
    one = [ctx(clock, "Truly a.", "A.")]
    assert not propose(tenant_id=T1, edits=one, existing_cases=[], known=[], clock=clock).proposed
    facts = [ctx(clock, "Costs 5.", "Costs 6."), ctx(clock, "Costs 5 now.", "Costs 6 now.")]
    assert not propose(tenant_id=T1, edits=facts, existing_cases=[], known=[], clock=clock).proposed


def test_rule_that_flags_an_old_human_final_is_withheld(clock):
    old = RegressionCase(id=new_id(clock), tenant_id=T1, created_at=clock.now(), kind=CaseKind.GOLDEN,
                         final="Truly, the owner said it best.")
    edits = [ctx(clock, "Truly a.", "A."), ctx(clock, "Truly b.", "B.")]
    rnd = propose(tenant_id=T1, edits=edits, existing_cases=[old], known=[], clock=clock)
    assert not rnd.proposed and "regression case" in rnd.withheld[0].reason


def test_known_or_declined_rules_are_not_proposed_again(clock):
    edits = [ctx(clock, "Truly a.", "A."), ctx(clock, "Truly b.", "B.")]
    first = propose(tenant_id=T1, edits=edits, existing_cases=[], known=[], clock=clock).proposed[0].proposal
    more = [ctx(clock, "Truly c.", "C."), ctx(clock, "Truly d.", "D.")]
    assert not propose(tenant_id=T1, edits=more, existing_cases=[], known=[first], clock=clock).proposed


def test_other_tenants_edits_never_count(clock):
    edits = [ctx(clock, "Truly a.", "A."), ctx(clock, "Truly b.", "B.", tenant=T2)]
    assert not propose(tenant_id=T1, edits=edits, existing_cases=[], known=[], clock=clock).proposed


# ---------------------------------------------------------------- lifecycle


def _proposal(clock, pillar="craft"):
    edits = [ctx(clock, "Truly a.", "A.", pillar=pillar), ctx(clock, "Truly b.", "B.", pillar=pillar)]
    return propose(tenant_id=T1, edits=edits, existing_cases=[], known=[], clock=clock).proposed[0].proposal


def test_accept_needs_a_live_human(clock, verifier, human, approver):
    p = _proposal(clock)
    with pytest.raises(HumanOnlyViolation):
        accept(p, actor=AgentActor(id="bot"), verifier=verifier, approver=approver, clock=clock)
    with pytest.raises(HumanOnlyViolation):
        accept(p, actor=HumanActor(id=USER, tenant_id=T1, session_id="token"), verifier=verifier,
               approver=approver, clock=clock)
    with pytest.raises(HumanOnlyViolation):
        accept(p, actor=HumanActor(id=USER, tenant_id=T2, session_id="live"), verifier=verifier,
               approver=approver, clock=clock)
    done = accept(p, actor=human, verifier=verifier, approver=approver, clock=clock)
    assert done.proposal.state is RuleProposalState.ACCEPTED
    assert done.approval.subject_kind is ApprovalSubject.RULE_PROPOSAL and done.approval.subject_id == p.id
    assert done.voice_rule.rule_proposal_id == p.id and done.voice_rule.scope_ref == "craft"


def test_approval_records_the_verified_person_and_checks_the_role(clock, verifier, human, approver):
    p = _proposal(clock)
    assert accept(p, actor=human, verifier=verifier, approver=approver, clock=clock).approval.approver_id == USER
    other = make_user(clock, user_id="0190f3a0-0000-7000-8000-0000000000bb")
    with pytest.raises(LifecycleError, match="not a known user"):
        accept(p, actor=human, verifier=verifier, approver=other, clock=clock)  # a different id is not recorded
    with pytest.raises(LifecycleError, match="not a known user"):
        accept(p, actor=human, verifier=verifier, approver=None, clock=clock)
    for role in (UserRole.SELLER, UserRole.STAFF):
        with pytest.raises(LifecycleError, match="may not approve"):
            accept(p, actor=human, verifier=verifier, approver=make_user(clock, role), clock=clock)
    with pytest.raises(LifecycleError, match="another tenant"):
        accept(p, actor=human, verifier=verifier, approver=make_user(clock, tenant=T2), clock=clock)


def test_reject_and_promote_apply_the_same_approver_check(clock, verifier, human, approver):
    p = _proposal(clock)
    bad = [None, make_user(clock, user_id="0190f3a0-0000-7000-8000-0000000000bb"),
           make_user(clock, UserRole.STAFF), make_user(clock, tenant=T2)]
    for user in bad:
        with pytest.raises(LifecycleError):
            reject(p, actor=human, verifier=verifier, approver=user)
        with pytest.raises(LifecycleError):
            promote(p, None, None, actor=human, verifier=verifier, approver=user, clock=clock)  # type: ignore[arg-type]
    assert reject(p, actor=human, verifier=verifier, approver=approver).state is RuleProposalState.REJECTED


def test_rejected_cannot_be_accepted(clock, verifier, human, approver):
    p = reject(_proposal(clock), actor=human, verifier=verifier, approver=approver)
    with pytest.raises(IllegalTransition):
        accept(p, actor=human, verifier=verifier, approver=approver, clock=clock)


def test_live_rules_respect_scope(clock, verifier, human, approver):
    done = accept(_proposal(clock), actor=human, verifier=verifier, approver=approver, clock=clock)
    assert live_rules([done.voice_rule], ctx(clock, "x", "y", pillar="craft"), clock)
    assert not live_rules([done.voice_rule], ctx(clock, "x", "y", pillar="offers"), clock)


def test_expiry_after_90_days_unused_on_fake_clock(clock, verifier, human, approver):
    done = accept(_proposal(clock), actor=human, verifier=verifier, approver=approver, clock=clock)
    clock.advance(EXPIRY - timedelta(seconds=1))
    assert expire_due(done.proposal, done.voice_rule, clock) is None
    clock.advance(seconds=1)
    assert expire_due(done.proposal, done.voice_rule, clock).state is RuleProposalState.EXPIRED
    assert expire_due(_proposal(clock), done.voice_rule, clock) is None  # a proposed rule cannot expire


def test_use_means_it_fired_on_a_draft(clock, verifier, human, approver):
    done = accept(_proposal(clock), actor=human, verifier=verifier, approver=approver, clock=clock)
    clock.advance(days=80)
    assert record_use(done.voice_rule, "Nothing to catch.", clock).last_used_at is None
    used = record_use(done.voice_rule, "Truly great.", clock)
    clock.advance(days=20)
    assert expire_due(done.proposal, used, clock) is None


def test_transfer_test_then_human_promotion(clock, verifier, human, approver):
    done = accept(_proposal(clock), actor=human, verifier=verifier, approver=approver, clock=clock)
    inside = ctx(clock, "Truly in.", "In.", pillar="craft")
    one = ctx(clock, "Truly x.", "X.", pillar="offers")
    res = transfer_test(done.proposal, done.voice_rule, [inside, one])
    assert not res.passed
    with pytest.raises(LifecycleError):
        promote(done.proposal, done.voice_rule, res, actor=human, verifier=verifier, approver=approver,
                clock=clock)
    two = ctx(clock, "Truly y.", "Y.", pillar="team")
    res = transfer_test(done.proposal, done.voice_rule, [inside, one, two])
    assert res.passed and inside.edit.id not in res.hits
    with pytest.raises(HumanOnlyViolation):
        promote(done.proposal, done.voice_rule, res, actor=AgentActor(id="bot"), verifier=verifier,
                approver=approver, clock=clock)
    p, rule = promote(done.proposal, done.voice_rule, res, actor=human, verifier=verifier, approver=approver,
                          clock=clock)
    assert p.state is RuleProposalState.PROMOTED and rule.scope is RuleScope.BRAND
    contra = ctx(clock, "Plain.", "Truly the best.", pillar="events")
    assert not transfer_test(done.proposal, done.voice_rule, [one, two, contra]).passed


# ---------------------------------------------------------------- eval harness and prompt registry


def _cases(clock, tenant=T1):
    return load_golden(GOLDEN_DIR / "demo_bakery.json", tenant_id=tenant, clock=clock)


GOOD = "Our sourdough is baked at dawn. A loaf is {{C-0001}} this weekend."
BAD = "Unlock the magic of our sourdough! A loaf is {{C-0001}} this weekend."


def test_code_checks_gate_the_run(clock):
    change = Change.of(ChangeKind.PROMPT, "draft@2", "body", "m")
    cases = _cases(clock)
    ok = run_eval(tenant_id=T1, change=change, cases=cases, generate=lambda c: GOOD, rules=[], clock=clock)
    assert ok.passed and ok.cases == 2
    bad = run_eval(tenant_id=T1, change=change, cases=cases, generate=lambda c: BAD, rules=[], clock=clock)
    assert not bad.passed and "regression" in bad.detail[1]["failures"]
    nums = run_eval(tenant_id=T1, change=change, cases=cases, generate=lambda c: "Loaf at 12 euros.",
                    rules=[], clock=clock)
    assert all("unslotted_number" in d["failures"] for d in nums.detail)
    empty = run_eval(tenant_id=T1, change=change, cases=[], generate=lambda c: GOOD, rules=[], clock=clock)
    assert not empty.passed


def test_live_voice_rule_breaks_the_case(clock, verifier, human, approver):
    done = accept(_proposal(clock, pillar=None), actor=human, verifier=verifier, approver=approver, clock=clock)
    change = Change.of(ChangeKind.PROMPT, "draft@2", "body", "m")
    run = run_eval(tenant_id=T1, change=change, cases=_cases(clock), generate=lambda c: "Truly. " + GOOD,
                   rules=[done.voice_rule], clock=clock)
    assert not run.passed and f"rule:{done.voice_rule.id}" in run.detail[0]["failures"]


def test_model_graders_through_the_registry(clock, registry):
    g = ModelGrader("voice", "Judge the quoted piece against the quoted approved final.", "a" * 64)
    change = Change.of(ChangeKind.PROMPT, "draft@2", "body", "m")
    cases = _cases(clock)
    ok = run_eval(tenant_id=T1, change=change, cases=cases, generate=lambda c: GOOD, rules=[], clock=clock,
                  graders=[g], registry=registry)
    assert ok.passed
    flagged = run_eval(tenant_id=T1, change=change, cases=cases[:1], generate=lambda c: "Unlock the magic.",
                       rules=[], clock=clock, graders=[g], registry=registry)
    assert flagged.flagged == 1 and not flagged.passed
    garbled = run_eval(tenant_id=T1, change=change, cases=cases[:1], generate=lambda c: "Fresh bread daily.",
                       rules=[], clock=clock, graders=[g], registry=registry)
    assert garbled.not_run == 1 and not garbled.passed
    dropped = run_eval(tenant_id=T1, change=change, cases=cases[:1], generate=lambda c: "Fresh bread daily.",
                       rules=[], clock=clock, graders=[g], registry=registry, dropped={"voice"})
    assert dropped.passed


def test_no_prompt_goes_live_on_a_failed_or_stale_run(clock):
    reg = PromptRegistry(clock)
    v1 = reg.register(tenant_id=T1, name="draft", role=TextRole.DRAFT, body="Write plainly.", model_id="m")
    assert reg.register(tenant_id=T1, name="draft", role=TextRole.DRAFT, body="Write plainly.", model_id="m") == v1
    v2 = reg.register(tenant_id=T1, name="draft", role=TextRole.DRAFT, body="Write warmly.", model_id="m")
    assert v2.version == 2 and reg.active(T1, "draft") is None
    cases = _cases(clock)
    sets = {T1: case_set_sha(cases)}
    with pytest.raises(ChangeBlocked):
        reg.activate(v2, [], sets)
    bad = run_eval(tenant_id=T1, change=reg.change(v2), cases=cases, generate=lambda c: BAD, rules=[], clock=clock)
    with pytest.raises(ChangeBlocked, match="failed"):
        reg.activate(v2, [bad], sets)
    clock.advance(minutes=1)
    good = run_eval(tenant_id=T1, change=reg.change(v2), cases=cases, generate=lambda c: GOOD, rules=[], clock=clock)
    newer = cases + [RegressionCase(id=new_id(clock), tenant_id=T1, created_at=clock.now(), kind=CaseKind.GOLDEN,
                                    final="Another approved piece.")]
    with pytest.raises(ChangeBlocked, match="stale"):
        reg.activate(v2, [bad, good], {T1: case_set_sha(newer)})
    other = run_eval(tenant_id=T1, change=reg.change(v1), cases=cases, generate=lambda c: GOOD, rules=[], clock=clock)
    with pytest.raises(ChangeBlocked):
        reg.activate(v2, [other], sets)  # a pass for another version proves nothing
    live = reg.activate(v2, [bad, good], sets)
    assert reg.active(T1, "draft") == live and live.eval_passed


def test_prompt_activation_needs_its_own_tenants_passing_run(clock):
    reg = PromptRegistry(clock)
    v = reg.register(tenant_id=T1, name="draft", role=TextRole.DRAFT, body="Write plainly.", model_id="m")
    c1, c2 = _cases(clock, T1), _cases(clock, T2)
    other = run_eval(tenant_id=T2, change=reg.change(v), cases=c2, generate=lambda c: GOOD, rules=[], clock=clock)
    assert other.passed
    with pytest.raises(ChangeBlocked, match="own client"):
        reg.activate(v, [other], {T2: case_set_sha(c2)})
    assert reg.active(T1, "draft") is None
    w = reg.register(tenant_id=T2, name="draft", role=TextRole.DRAFT, body="Write plainly.", model_id="m")
    assert reg.change(v).sha256 != reg.change(w).sha256  # same body, different client, different change
    own = run_eval(tenant_id=T1, change=reg.change(v), cases=c1, generate=lambda c: GOOD, rules=[], clock=clock)
    assert reg.activate(v, [own, other], {T1: case_set_sha(c1)}).eval_passed


def test_shared_change_needs_every_client_to_pass(clock):
    change = Change.of(ChangeKind.TEMPLATE, "carousel-3", "<html>")
    c1, c2 = _cases(clock, T1), _cases(clock, T2)
    r1 = run_eval(tenant_id=T1, change=change, cases=c1, generate=lambda c: GOOD, rules=[], clock=clock)
    r2 = run_eval(tenant_id=T2, change=change, cases=c2, generate=lambda c: BAD, rules=[], clock=clock)
    d = gate(change, [r1, r2], {T1: case_set_sha(c1), T2: case_set_sha(c2)})
    assert not d.allowed and any(T2 in r for r in d.reasons)
    assert not gate(change, [r1], {}).allowed


# ---------------------------------------------------------------- blind comparison


def _session(clock):
    pieces = [Piece(BlindSource.THALLO, f"b{i}", f"thallo {i}") for i in range(3)] + \
             [Piece(BlindSource.AGENCY, f"b{i}", f"agency {i}") for i in range(3)]
    return BlindSession(tenant_id=T1, pieces=pieces, clock=clock, seed=7)


ALL4 = dict.fromkeys(Dimension, 4)


def test_blind_presents_no_source_and_reveals_only_after_scoring(clock):
    s = _session(clock)
    shown = s.presented()
    assert all(not hasattr(p, "source") for p in shown)
    assert len({p.ref for p in shown}) == 6
    with pytest.raises(BlindError):
        s.score(rater_id=USER, ref=shown[0].ref, scores={Dimension.CRAFT: 3})
    for p in shown[:-1]:
        s.score(rater_id=USER, ref=p.ref, scores=ALL4)
    with pytest.raises(BlindError, match="1 left"):
        s.reveal(USER)
    with pytest.raises(BlindError, match="final"):
        s.score(rater_id=USER, ref=shown[0].ref, scores=ALL4)
    s.score(rater_id=USER, ref=shown[-1].ref, scores=ALL4)
    assert {src for _, src, _ in s.reveal(USER)} == set(BlindSource)


def test_blind_verdict(clock):
    s = _session(clock)
    with pytest.raises(BlindError):
        s.verdict([USER])
    for item, p in zip(s.items, s.presented(), strict=True):
        n = 5 if item.source is BlindSource.THALLO else 3
        s.score(rater_id=USER, ref=p.ref, scores=dict.fromkeys(Dimension, n))
    v = s.verdict([USER])
    assert v.at_least_level and v.means[BlindSource.AGENCY][Dimension.CRAFT] == 3


def test_blind_needs_both_sources(clock):
    with pytest.raises(ValueError):
        BlindSession(tenant_id=T1, pieces=[Piece(BlindSource.THALLO, "b", "t")], clock=clock)


# ---------------------------------------------------------------- approval rate never alone


def test_approval_rate_needs_outcomes_beside_it():
    with pytest.raises(ValueError):
        ApprovalWithOutcomes([])
    s = ApprovalWithOutcomes([PieceOutcome("v1", True, "2 enquiries"), PieceOutcome("v2", False, None)])
    assert s.approval_rate == 0.5
    assert "outcome missing" in s.lines()[2] and "2 enquiries" in s.lines()[1]
