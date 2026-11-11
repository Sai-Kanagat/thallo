"""The hash-chained audit log detects any edit, deletion, insertion or reorder."""
from __future__ import annotations

import pytest

from cortex_operator.contracts.actors import ActorRef, AgentActor, HumanActor, SystemActor
from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.enums import ActorKind
from cortex_operator.trust.audit import (
    AuditConflict,
    AuditDraft,
    AuditError,
    AuditLog,
    InMemoryAuditStore,
    compute_entry_hash,
    verify_chain,
)
from tests.wp04_gate.conftest import ident

T = "019b8d62-7a80-7000-8000-00000000aaaa"
U = "019b8d62-7a80-7000-8000-00000000bbbb"


@pytest.fixture
def log():
    clock = FakeClock(FAKE_EPOCH)
    log = AuditLog(InMemoryAuditStore(), clock)
    person = HumanActor(id=ident(clock), tenant_id=T, session_id="secret-session")
    for i in range(5):
        clock.advance(minutes=1)
        log.append(T, person if i % 2 else SystemActor(id="worker"), f"action.{i}", subject_kind="bundle",
                   subject_id=str(i), payload={"n": i, "note": "x"})
    return log


def entries(log):
    return list(log.store.entries(T))


def test_a_clean_chain_verifies(log):
    es = entries(log)
    assert [e.seq for e in es] == [1, 2, 3, 4, 5]
    assert es[0].prev_hash is None and all(b.prev_hash == a.entry_hash for a, b in zip(es, es[1:], strict=False))
    report = log.verify(T, anchor=log.head(T))
    assert report.ok and report.head_seq == 5 and report.head_hash == es[-1].entry_hash


def test_entries_carry_an_actor_ref_never_a_session(log):
    es = entries(log)
    assert {e.actor_kind for e in es} == {ActorKind.HUMAN, ActorKind.SYSTEM}
    assert "secret-session" not in repr([e.model_dump() for e in es])


@pytest.mark.parametrize("payload", [{"session_id": "s"}, {"a": {"Session": "s"}}, {"x": 1.5},
                                     {"x": object()}, {1: "a"}])
def test_bad_payloads_are_refused(log, payload):
    with pytest.raises(AuditError):
        log.append(T, SystemActor(id="w"), "x", payload=payload)


@pytest.mark.parametrize("field,value", [
    ("action", "approval.granted"), ("actor_id", "someone-else"), ("payload", {"n": 99, "note": "x"}),
    ("subject_id", "9"), ("actor_kind", ActorKind.AGENT),
])
def test_an_edit_is_detected(log, field, value):
    es = entries(log)
    es[2] = es[2].model_copy(update={field: value})
    r = verify_chain(es, tenant_id=T)
    assert not r.ok and any(p.seq == 3 and "edited" in p.problem for p in r.problems)


def test_an_edit_with_a_recomputed_hash_breaks_the_next_link(log):
    es = entries(log)
    forged = es[2].model_copy(update={"action": "approval.granted"})
    es[2] = forged.model_copy(update={"entry_hash": compute_entry_hash(forged)})
    r = verify_chain(es, tenant_id=T)
    assert not r.ok and any(p.seq == 4 and "prev_hash" in p.problem for p in r.problems)


@pytest.mark.parametrize("index", [0, 2, 4])
def test_a_deletion_is_detected(log, index):
    es = entries(log)
    anchor = log.head(T)
    del es[index]
    assert not verify_chain(es, tenant_id=T, anchor=anchor).ok


def test_tail_truncation_needs_the_anchor(log):
    anchor = log.head(T)
    es = entries(log)[:-1]
    assert verify_chain(es, tenant_id=T).ok  # documented limit: a shorter chain is self-consistent
    assert not verify_chain(es, tenant_id=T, anchor=anchor).ok


def test_a_rewritten_history_fails_the_anchor(log):
    anchor = log.head(T)
    clock = FakeClock(FAKE_EPOCH)
    fresh = AuditLog(InMemoryAuditStore(), clock)
    for i in range(5):
        fresh.append(T, SystemActor(id="forger"), f"action.{i}")
    assert fresh.verify(T).ok and not fresh.verify(T, anchor=anchor).ok


def test_a_reorder_is_detected(log):
    es = entries(log)
    es[1], es[2] = es[2], es[1]
    assert not verify_chain(es, tenant_id=T).ok


def test_an_insertion_is_detected(log):
    es = entries(log)
    es.insert(2, es[1])
    assert not verify_chain(es, tenant_id=T).ok


def test_another_tenants_entry_is_detected(log):
    other = AuditLog(InMemoryAuditStore(), FakeClock(FAKE_EPOCH))
    foreign = other.append(U, SystemActor(id="w"), "x")
    es = entries(log)
    es[0] = foreign
    r = verify_chain(es, tenant_id=T)
    assert not r.ok and any("another tenant" in p.problem for p in r.problems)


def test_tenants_have_separate_chains(log):
    e = log.append(U, ActorRef.of(AgentActor(id="wp-07")), "draft.made")
    assert e.seq == 1 and e.prev_hash is None
    assert log.verify(U).ok and log.verify(T).ok


def test_a_concurrent_append_conflicts_instead_of_forking(log):
    es = entries(log)
    with pytest.raises(AuditConflict):
        log.store.insert(es[-1].model_copy(update={"action": "racing"}))


def test_an_empty_chain_verifies():
    assert verify_chain([], tenant_id=T).ok


@pytest.mark.parametrize("kw", [
    {"payload": {"reason": "copied from session abc"}},
    {"payload": {"notes": ["fine", "SESSION=x"]}},
    {"subject_id": "session-1"},
    {"subject_kind": "user_session"},
])
def test_session_is_refused_in_free_text_too(log, kw):
    with pytest.raises(AuditError):
        log.append(T, SystemActor(id="w"), "x", **kw)


def test_session_is_refused_in_the_action(log):
    with pytest.raises(AuditError):
        log.append(T, SystemActor(id="w"), "session.leak")


def test_prepare_returns_chained_entries_without_writing(log):
    head = log.head(T)
    drafts = log.prepare(T, [AuditDraft(SystemActor(id="w"), "a"), AuditDraft(SystemActor(id="w"), "b", "x", "1")])
    assert log.head(T) == head  # nothing written yet
    assert [d.seq for d in drafts] == [6, 7] and drafts[0].prev_hash == head[1]
    assert drafts[1].prev_hash == drafts[0].entry_hash
    for d in drafts:
        log.store.insert(d)
    assert log.verify(T).ok
