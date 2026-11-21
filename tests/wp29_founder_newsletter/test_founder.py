"""Founder desk: only the founder's own answers, the own-words check, and no send path."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.wp29_founder_newsletter.conftest import GHOST, POST, SAID, answer_of
from thallo.capture.records import TranscriptState
from thallo.contracts.enums import Channel, CheckVerdict, UserRole
from thallo.contracts.states import VariantState
from thallo.copy import CopyStatus
from thallo.extensions.founder import FounderError, draft_profile_post, own_words, own_words_share


def draft(registry_for, tenant, founder, clock, answers, *texts):
    reg, script = registry_for(*texts)
    out = draft_profile_post(registry=reg, tenant=tenant, founder=founder, answers=answers, claims={}, clock=clock,
                             resolver=lambda u: None, non_links=())
    return out, script


def test_a_post_from_the_founders_answer_waits_for_the_gate(registry_for, tenant, founder, clock):
    a = answer_of(clock, founder.id)
    out, script = draft(registry_for, tenant, founder, clock, [a], POST, POST, POST)
    assert out.status is CopyStatus.READY_FOR_CHECKS
    v = out.variant
    assert v.channel is Channel.LINKEDIN_PERSONAL and v.state is VariantState.CHECKING
    assert out.post.answer_ids == [a.answer.id] and out.post.founder_id == founder.id
    check = out.extra_checks[0]
    assert check.check_name == "founder.own_words" and check.verdict is CheckVerdict.PASS
    assert check.content_sha256 == out.copy.check_results[0].content_sha256
    # the answer travels as data, never in the instructions
    assert SAID not in script.system(0)
    rows = out.rows()
    assert rows[0] is out.idea and rows[1] is out.brief and rows[-1] is out.post


def test_ghost_views_fail_and_go_to_a_person(registry_for, tenant, founder, clock):
    out, _ = draft(registry_for, tenant, founder, clock, [answer_of(clock, founder.id)], GHOST, GHOST, GHOST)
    assert out.extra_checks[0].verdict is CheckVerdict.FAIL
    assert out.variant.state is VariantState.DRAFTING and out.status is CopyStatus.NEEDS_HUMAN


def test_someone_elses_answer_is_refused_before_any_call(registry_for, tenant, founder, clock):
    other = answer_of(clock, "0" * 8 + "-0000-7000-8000-" + "0" * 12)
    with pytest.raises(FounderError):
        draft(registry_for, tenant, founder, clock, [other], POST)


def test_a_non_founder_and_an_untranscribed_answer_are_refused(registry_for, tenant, founder, clock):
    seller = founder.model_copy(update={"role": UserRole.SELLER})
    with pytest.raises(FounderError):
        draft(registry_for, tenant, seller, clock, [answer_of(clock, founder.id)], POST)
    with pytest.raises(FounderError):
        draft(registry_for, tenant, founder, clock, [answer_of(clock, founder.id, state=TranscriptState.PENDING)], POST)


def test_no_answers_no_draft_no_call(registry_for, tenant, founder, clock):
    out, script = draft(registry_for, tenant, founder, clock, [])
    assert out.status is CopyStatus.NO_SOURCE and out.variant is None and out.rows() == []
    assert script.calls == []


def test_own_words_thresholds():
    assert own_words(POST, [SAID])[0] is CheckVerdict.PASS
    assert own_words(GHOST, [SAID])[0] is CheckVerdict.FAIL
    assert own_words_share("", [SAID]) == 0.0
    assert own_words("{{C-0001}} frame chain customer markets investors portfolios", [SAID])[0] is CheckVerdict.FLAG


def test_the_founder_desk_builds_no_message_or_outreach():
    """LinkedIn DMs and connection requests are forbidden: the package names no messaging at all."""
    root = Path(__file__).resolve().parents[2] / "thallo" / "extensions" / "founder"
    for f in root.glob("*.py"):
        tree = ast.parse(f.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not ({"MessageSendRequest", "PublishRequest"} & (names | attrs)), f.name
        assert not any("messaging" in m or "publish" in m or "leads" in m for m in mods), f.name
