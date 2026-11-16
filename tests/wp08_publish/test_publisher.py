"""The publisher end to end, on memory and on Postgres (per-run app role): it refuses on every gate
refusal and sends nothing, sends exactly the approved bytes once, never resends an unknown outcome,
reconciles a crash without a second call, retries with backoff only what certainly did not go out,
and reads back what landed."""
# ruff: noqa: F811  (backend, shop, signer and pg_urls are fixtures imported from the wave 1 flow)
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from tests.integration.conftest import pg_urls  # noqa: F401
from tests.integration.test_check_binding import ChecksDown
from tests.integration.test_wave1_flow import IMAGE, _id, backend, shop, signer  # noqa: F401
from thallo.contracts.actors import HumanActor
from thallo.contracts.enums import CheckVerdict
from thallo.contracts.models import Bundle, CheckResult, EventRecord, Publication, Variant
from thallo.contracts.states import VariantState
from thallo.gate import BlockReason, void_bundle_approvals
from thallo.gate.approvals import DEFAULT_APPROVAL_TTL
from thallo.publish import PublicationSend, SendState, Status, WaitingOn
from thallo.publish.publisher import LEASE

from .harness import build_rig


class Verifier:
    def __init__(self, live: str) -> None:
        self.live = live

    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id == self.live


@pytest.fixture
def rig(backend, shop, signer):
    return build_rig(backend, shop, signer)


def rows(rig, model, **where):
    with rig.backend.session(rig.t) as s:
        return s.list(model, **where)


def send_row(rig) -> PublicationSend:
    (pub,) = rows(rig, Publication)
    with rig.backend.session(rig.t) as s:
        return s.get(PublicationSend, pub.id)


def variant(rig) -> Variant:
    with rig.backend.session(rig.t) as s:
        return s.get(Variant, rig.shop.variant.id)


def events(rig, name: str) -> list[EventRecord]:
    return [e for e in rows(rig, EventRecord) if e.name == name]


def person(rig):
    return HumanActor(id=rig.shop.founder.id, tenant_id=rig.t, session_id="live"), Verifier("live")


# ---------------------------------------------------------------- the happy path

def test_sends_exactly_the_approved_payload_once(rig):
    out = rig.publish()
    assert out.status is Status.SENT, out
    (call,) = rig.poster.calls
    payload = rig.shop.approved_payload
    assert call.text == payload.text and call.media_sha256 == payload.media_sha256
    assert call.content_hash == rig.shop.approval.content_hash and call.approval_id == rig.shop.approval.id
    assert rig.poster.uploads == [hashlib.sha256(IMAGE).hexdigest()]  # the bytes the provider got
    (pub,) = rows(rig, Publication)
    assert pub.idempotency_key == f"{rig.shop.variant.id}:{rig.shop.approval.id}" == call.idempotency_key
    assert pub.content_sha256 == payload.variant_hash and pub.external_id == "post-1" and pub.sent_at
    assert pub.connection_id is not None and send_row(rig).state is SendState.SENT
    assert variant(rig).state is VariantState.PUBLISHED
    assert len(events(rig, "publication.sent")) == 1


def test_a_second_publish_is_already_sent_and_calls_nothing(rig):
    rig.publish()
    again = rig.publish()
    assert again.status is Status.ALREADY_SENT and again.waiting_on is WaitingOn.NOBODY
    assert len(rig.poster.calls) == 1 and len(rows(rig, Publication)) == 1


# ---------------------------------------------------------------- refusals send nothing

def assert_refused(rig, out, reason: str):
    assert out.status is Status.BLOCKED and reason in out.reasons, out
    assert out.next_step and out.waiting_on is WaitingOn.PERSON
    assert rig.poster.calls == [] and rows(rig, Publication) == []
    assert any(reason in e.payload["reason"] for e in events(rig, "publication.blocked"))


def test_a_fail_for_the_content_being_sent_blocks(rig):
    seeded = rows(rig, CheckResult)[0]
    with rig.backend.session(rig.t) as s:
        s.insert(seeded.model_copy(update={"id": _id(rig.backend), "verdict": CheckVerdict.FAIL,
                                           "attempt": seeded.attempt + 5, "reason": "late"}))
    assert_refused(rig, rig.publish(), BlockReason.CHECK_FAILED.value)


class NoRows:
    def __init__(self, gate) -> None:
        self._gate = gate

    def __getattr__(self, name):
        return getattr(self._gate, name)

    def get_check_results(self, tenant_id, variant_id, content_sha256):
        return []


def test_missing_coverage_blocks(rig):
    rig.gate_wrap = NoRows
    assert_refused(rig, rig.publish(), BlockReason.NOT_CHECKED.value)


def test_unreadable_checks_block(rig):
    rig.gate_wrap = ChecksDown
    assert_refused(rig, rig.publish(), BlockReason.CHECKS_UNREADABLE.value)


def test_an_expired_approval_blocks_and_goes_back_to_the_checks(rig):
    rig.backend.clock.advance(DEFAULT_APPROVAL_TTL + timedelta(seconds=1))
    assert_refused(rig, rig.publish(), BlockReason.APPROVAL_EXPIRED.value)
    assert variant(rig).state is VariantState.CHECKING


def test_a_voided_approval_blocks(rig):
    with rig.backend.session(rig.t) as s:
        s.write(void_bundle_approvals(s.get(Bundle, rig.shop.bundle.id), s.list(Variant, bundle_id=rig.shop.bundle.id),
                                      s.gate.get_approvals(rig.t, rig.shop.bundle.id), reason="caption edited",
                                      actor=rig.shop.human, clock=rig.backend.clock, audit=s.audit, signer=rig.signer))
    assert_refused(rig, rig.publish(), BlockReason.APPROVAL_VOIDED.value)


def test_no_gate_key_blocks(rig, monkeypatch):
    rig.publisher.signer = None
    monkeypatch.delenv("THALLO_GATE_SIGNING_KEY", raising=False)
    assert_refused(rig, rig.publish(), BlockReason.GATE_KEY_MISSING.value)


def test_another_tenants_channel_account_or_a_wrong_one_blocks(rig):
    assert_refused(rig, rig.publish(channel_account_id=_id(rig.backend)), "channel_account")


class SwapsBytesAfterTheCheck:
    """The gate reads the file once (approval hash holds); the publisher's own read, a moment
    later, gets different bytes, as if the file changed between the check and the upload."""

    def __init__(self, gate) -> None:
        self._gate = gate
        self.reads = 0

    def __getattr__(self, name):
        return getattr(self._gate, name)

    def read_media_bytes(self, tenant_id, media):
        self.reads += 1
        data = self._gate.read_media_bytes(tenant_id, media)
        return data if self.reads == 1 else data + b" altered"


def test_bytes_on_disk_that_differ_from_the_payload_block(rig):
    rig.gate_wrap = SwapsBytesAfterTheCheck
    out = rig.publish()
    assert_refused(rig, out, "media_bytes_changed")
    assert variant(rig).state is VariantState.CHECKING


# ---------------------------------------------------------------- unknown outcomes are never resent

def test_a_crash_between_send_and_record_is_reconciled_without_a_resend(rig, monkeypatch):
    def crash(*a, **k):
        raise RuntimeError("process killed after the send")
    monkeypatch.setattr(rig.publisher, "_record_result", crash)
    with pytest.raises(RuntimeError):
        rig.publish()
    assert len(rig.poster.calls) == 1 and send_row(rig).state is SendState.SENDING
    monkeypatch.undo()
    assert rig.publish().status is Status.IN_FLIGHT  # inside the lease: someone may still be sending
    rig.backend.clock.advance(LEASE + timedelta(seconds=1))
    assert rig.publish().status is Status.NEEDS_CHECK  # the claimant died: never resend
    assert len(rig.poster.calls) == 1
    out = rig.publisher.reconcile(rig.t, rig.shop.variant.id)
    assert out.status is Status.SENT and out.external_id == "post-1"
    assert len(rig.poster.calls) == 1 and rig.poster.readbacks == 1
    (pub,) = rows(rig, Publication)
    assert pub.external_id == "post-1" and send_row(rig).state is SendState.SENT
    assert variant(rig).state is VariantState.PUBLISHED


def test_a_timeout_needs_a_check_and_is_never_resent(rig):
    rig.poster.behaviour = "timeout"
    assert rig.publish().status is Status.NEEDS_CHECK
    assert rig.publish().status is Status.NEEDS_CHECK
    assert len(rig.poster.calls) == 1 and variant(rig).state is VariantState.SCHEDULED


def test_a_reconcile_that_finds_nothing_holds_for_a_person(rig):
    rig.poster.behaviour = "timeout"
    rig.publish()
    rig.poster.posts.clear()  # the service has no such post
    out = rig.publisher.reconcile(rig.t, rig.shop.variant.id)
    assert out.status is Status.NEEDS_CHECK and out.waiting_on is WaitingOn.PERSON
    assert len(rig.poster.calls) == 1
    # A person looked and nothing went out: only then may the same bytes go again.
    actor, verifier = person(rig)
    shown = rig.publisher.status(rig.t, rig.shop.variant.id)
    settled = rig.publisher.settle(rig.t, rig.shop.variant.id, actor=actor, verifier=verifier, posted=False,
                                   attempt=shown.attempt, requested_at=shown.requested_at)
    assert settled.status is Status.RETRY_LATER
    rig.poster.behaviour = "ok"
    assert rig.publish().status is Status.SENT and len(rig.poster.calls) == 2


def test_a_provider_that_accepts_without_an_id_needs_a_check(rig):
    rig.poster.behaviour = "no_id"
    assert rig.publish().status is Status.NEEDS_CHECK


# ---------------------------------------------------------------- retry with backoff, only when nothing went out

def test_not_sent_is_retried_with_backoff(rig):
    rig.poster.behaviour = "not_sent_retry"
    first = rig.publish()
    assert first.status is Status.RETRY_LATER and variant(rig).state is VariantState.FAILED
    assert rig.publish().status is Status.RETRY_LATER and len(rig.poster.calls) == 1  # not yet due
    rig.backend.clock.advance(minutes=5)
    rig.poster.behaviour = "ok"
    assert rig.publish().status is Status.SENT
    assert len(rig.poster.calls) == 2 and send_row(rig).attempts == 2
    assert len(rows(rig, Publication)) == 1


def test_backoff_doubles_and_stops_after_the_last_try(rig):
    rig.poster.behaviour = "not_sent_retry"
    gaps = []
    for _ in range(4):
        rig.publish()
        s = send_row(rig)
        gaps.append(s.next_try_at - s.updated_at)
        rig.backend.clock.advance(s.next_try_at - rig.backend.clock.now() + timedelta(seconds=1))
    assert [g.total_seconds() / 60 for g in gaps] == pytest.approx([5, 10, 20, 40], abs=0.1)
    assert rig.publish().status is Status.FAILED  # the fifth try
    assert rig.publish().status is Status.FAILED and len(rig.poster.calls) == 5


def test_a_refusal_that_means_no_post_is_final(rig):
    rig.poster.behaviour = "not_sent_final"
    assert rig.publish().status is Status.FAILED
    assert rig.publish().status is Status.FAILED and len(rig.poster.calls) == 1


# ---------------------------------------------------------------- review fixes: settle and late results

def test_settle_during_an_active_lease_is_refused_and_nothing_is_resent(rig, monkeypatch):
    monkeypatch.setattr(rig.publisher, "_record_result", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        rig.publish()
    monkeypatch.undo()
    s = send_row(rig)
    actor, verifier = person(rig)
    for posted in (False, True):
        out = rig.publisher.settle(rig.t, rig.shop.variant.id, actor=actor, verifier=verifier, posted=posted,
                                   live_url="https://social.invalid/p/x", attempt=s.attempts,
                                   requested_at=s.requested_at)
        assert out.status is Status.BLOCKED and "not_open" in out.reasons
    assert send_row(rig).state is SendState.SENDING
    assert rig.publish().status is Status.IN_FLIGHT and len(rig.poster.calls) == 1


def test_settle_quoting_an_older_attempt_is_refused(rig):
    rig.poster.behaviour = "timeout"
    rig.publish()
    s = send_row(rig)
    actor, verifier = person(rig)
    out = rig.publisher.settle(rig.t, rig.shop.variant.id, actor=actor, verifier=verifier, posted=False,
                               attempt=s.attempts + 1, requested_at=s.requested_at)
    assert out.status is Status.BLOCKED and send_row(rig).state is SendState.NEEDS_CHECK


def test_settle_needs_the_signed_in_person(rig):
    rig.poster.behaviour = "timeout"
    rig.publish()
    s = send_row(rig)
    actor, _ = person(rig)
    out = rig.publisher.settle(rig.t, rig.shop.variant.id, actor=actor, verifier=Verifier("other"), posted=False,
                               attempt=s.attempts, requested_at=s.requested_at)
    assert out.status is Status.BLOCKED and "not_a_person" in out.reasons


def move_row_during_send(rig, **fields):
    def hook():
        (pub,) = rows(rig, Publication)
        with rig.backend.session(rig.t) as s:
            s.update(PublicationSend, pub.id, **fields)
    rig.poster.on_send = hook


@pytest.mark.parametrize("behaviour", ["ok", "not_sent_retry", "timeout"])
def test_a_state_change_during_the_send_is_a_conflict_and_writes_nothing_else(rig, behaviour):
    rig.poster.behaviour = behaviour
    move_row_during_send(rig, state=SendState.NEEDS_CHECK, detail="lease expired, reconciler took it")
    out = rig.publish()
    assert out.status is Status.NEEDS_CHECK and "conflict" in out.reasons and out.waiting_on is WaitingOn.PERSON
    (pub,) = rows(rig, Publication)
    assert pub.external_id is None and pub.live_url is None and pub.sent_at is None
    assert send_row(rig).state is SendState.NEEDS_CHECK
    assert variant(rig).state is VariantState.SCHEDULED  # neither PUBLISHED nor FAILED
    if behaviour == "ok":
        assert out.external_id == "post-1"  # what came back is kept for the person
    assert any("conflict" in e.payload["reason"] for e in events(rig, "publication.blocked"))
    assert len(rig.poster.calls) == 1


def test_a_late_result_of_attempt_one_leaves_attempt_twos_row_alone(rig):
    later = rig.backend.clock.now() + timedelta(minutes=30)
    move_row_during_send(rig, state=SendState.SENDING, attempts=2, requested_at=later)  # attempt 2 started
    out = rig.publish()
    assert out.status is Status.NEEDS_CHECK and "conflict" in out.reasons
    s = send_row(rig)
    assert (s.state, s.attempts, s.requested_at) == (SendState.SENDING, 2, later)  # untouched
    (pub,) = rows(rig, Publication)
    assert pub.external_id is None
    assert len(rig.poster.calls) == 1


def test_a_late_result_never_pulls_a_sent_row_back(rig):
    move_row_during_send(rig, state=SendState.SENT, detail="found by reconcile")
    rig.poster.behaviour = "timeout"
    rig.publish()
    assert send_row(rig).state is SendState.SENT


# ---------------------------------------------------------------- readback

def test_readback_ok(rig):
    rig.publish()
    out = rig.publisher.readback(rig.t, rig.shop.variant.id)
    assert out.status is Status.READ_BACK_OK and not out.warnings
    (pub,) = rows(rig, Publication)
    assert pub.readback_ok is True and pub.readback_at and pub.readback_diff is None
    assert variant(rig).state is VariantState.READ_BACK_OK
    assert len(events(rig, "publication.read_back")) == 1


def test_a_text_mismatch_is_recorded_and_surfaced(rig):
    rig.publish()
    rig.poster.live["post-1"] = rig.shop.approved_payload.text.replace("120 km", "12O km")
    out = rig.publisher.readback(rig.t, rig.shop.variant.id)
    assert out.status is Status.READ_BACK_MISMATCH and out.waiting_on is WaitingOn.PERSON and "12O km" in out.detail
    (pub,) = rows(rig, Publication)
    assert pub.readback_ok is False and "12O km" in pub.readback_diff
    assert variant(rig).state is VariantState.READ_BACK_MISMATCH
    (event,) = events(rig, "publication.mismatch")
    assert "12O km" in event.payload["diff"]


def test_whitespace_the_canonical_form_ignores_is_not_a_mismatch(rig):
    rig.publish()
    rig.poster.live["post-1"] = "  " + rig.shop.approved_payload.text.replace(" ", "  ") + "\n\n"
    assert rig.publisher.readback(rig.t, rig.shop.variant.id).status is Status.READ_BACK_OK


def test_recompressed_media_is_a_warning_not_a_mismatch(rig):
    rig.publish()
    rig.poster.media_changed = True
    out = rig.publisher.readback(rig.t, rig.shop.variant.id)
    assert out.status is Status.READ_BACK_OK and out.warnings
    (pub,) = rows(rig, Publication)
    assert pub.readback_ok is True and pub.readback_diff.startswith("warning:")


def test_no_live_text_is_never_read_back_ok(rig):
    rig.publish()
    rig.poster.live["post-1"] = None
    out = rig.publisher.readback(rig.t, rig.shop.variant.id)
    assert out.status is Status.READBACK_PENDING
    (pub,) = rows(rig, Publication)
    assert pub.readback_ok is None and variant(rig).state is VariantState.PUBLISHED


# ---------------------------------------------------------------- the manual floor

def test_with_no_real_connection_the_manual_pack_waits_on_a_person(backend, shop, signer):
    rig = build_rig(backend, shop, signer, fake=False)
    out = rig.publish()
    assert out.status is Status.MANUAL_PENDING and out.waiting_on is WaitingOn.PERSON
    assert rig.publish().status is Status.MANUAL_PENDING  # never a second pack
    actor, verifier = person(rig)
    shown = rig.publisher.status(rig.t, rig.shop.variant.id)
    done = rig.publisher.settle(rig.t, rig.shop.variant.id, actor=actor, verifier=verifier, posted=True,
                                live_url="https://instagram.invalid/p/1", attempt=shown.attempt,
                                requested_at=shown.requested_at)
    assert done.status is Status.SENT and variant(rig).state is VariantState.PUBLISHED
    assert rig.publisher.readback(rig.t, rig.shop.variant.id).status is Status.READBACK_PENDING  # a person reads
    got = rig.publisher.record_readback(rig.t, rig.shop.variant.id, actor=actor, verifier=verifier,
                                        live_text=rig.shop.approved_payload.text, media_changed=None)
    assert got.status is Status.READ_BACK_OK


def test_the_chain_skips_an_unconfigured_and_an_unhealthy_connection_and_falls_to_manual(backend, shop, signer):
    from thallo.connections.stores import InMemoryHealthState
    from thallo.contracts.enums import Capability
    from thallo.providers.publishing import postiz_hosted

    from .harness import FAKE, mirror_connections

    rig = build_rig(backend, shop, signer)  # the fake poster is configured first
    rig.registry.health = InMemoryHealthState()
    (fake_row,) = [r for r in rig.registry.connections.list_for_tenant(rig.t) if r.provider == FAKE]
    rig.registry.health.set_connected(rig.t, fake_row.id, False)  # last seen unhealthy
    postiz = rig.registry.add(rig.t, Capability.PUBLISHING, postiz_hosted.NAME, priority=-1)  # no key, no channels
    mirror_connections(rig)
    out = rig.publish()
    assert out.status is Status.MANUAL_PENDING and rig.poster.calls == []
    (pub,) = rows(rig, Publication)
    assert pub.connection_id not in (fake_row.id, postiz.id)  # pinned to the manual floor


# ---------------------------------------------------------------- more refusals and holds

def test_a_payload_mismatch_from_the_gate_blocks_and_goes_back_to_the_checks(rig, monkeypatch):
    import thallo.publish.publisher as publisher_module
    from thallo.gate.decision import Block, GateDecision

    def mismatch(repo, **kw):
        return GateDecision(allowed=False, blocks=(Block(BlockReason.PAYLOAD_MISMATCH, "not the hashed content"),),
                            tenant_id=kw["tenant_id"], bundle_id=kw["bundle_id"], variant_id=kw["variant_id"],
                            approval_id=kw["approval_id"])
    monkeypatch.setattr(publisher_module, "check_publish", mismatch)
    assert_refused(rig, rig.publish(), BlockReason.PAYLOAD_MISMATCH.value)
    assert variant(rig).state is VariantState.CHECKING


def test_an_ambiguous_reconcile_holds_for_a_person(rig):
    from thallo.publish import AmbiguousLookup

    rig.poster.behaviour = "timeout"
    rig.publish()

    def ambiguous(*a, **k):
        raise AmbiguousLookup("more than one post matches")
    rig.poster.run = ambiguous
    out = rig.publisher.reconcile(rig.t, rig.shop.variant.id)
    assert out.status is Status.NEEDS_CHECK and out.waiting_on is WaitingOn.PERSON


def test_a_reconcile_whose_connection_is_gone_holds_for_a_person(rig):
    rig.poster.behaviour = "timeout"
    rig.publish()
    (pub,) = rows(rig, Publication)
    rig.registry.connections.delete(rig.t, pub.connection_id)
    out = rig.publisher.reconcile(rig.t, rig.shop.variant.id)
    assert out.status is Status.NEEDS_CHECK and out.waiting_on is WaitingOn.PERSON


# ---------------------------------------------------------------- the way out of FAILED

def reopen_failed(rig):
    rig.poster.behaviour = "not_sent_final"
    assert rig.publish().status is Status.FAILED
    actor, verifier = person(rig)
    shown = rig.publisher.status(rig.t, rig.shop.variant.id)
    stale = rig.publisher.reopen(rig.t, rig.shop.variant.id, actor=actor, verifier=verifier,
                                 attempt=shown.attempt + 1, requested_at=shown.requested_at)
    assert stale.status is Status.BLOCKED and send_row(rig).state is SendState.FAILED
    out = rig.publisher.reopen(rig.t, rig.shop.variant.id, actor=actor, verifier=verifier,
                               attempt=shown.attempt, requested_at=shown.requested_at)
    assert out.status is Status.RETRY_LATER and send_row(rig).state is SendState.RETRY
    assert variant(rig).state is VariantState.FAILED  # reopening schedules nothing
    rig.poster.behaviour = "ok"


def test_a_failed_send_reopens_to_retry_and_goes_through_the_gate_again(rig):
    reopen_failed(rig)
    assert rig.publish().status is Status.SENT and len(rig.poster.calls) == 2


def test_reopen_after_a_kill_switch_sends_and_schedules_nothing(rig):
    reopen_failed(rig)
    rig.backend.set_paused(True)
    try:
        out = rig.publish()
    finally:
        rig.backend.set_paused(False)
    assert out.status is Status.BLOCKED and BlockReason.SYSTEM_PAUSED.value in out.reasons
    assert len(rig.poster.calls) == 1 and variant(rig).state is VariantState.FAILED
    assert send_row(rig).state is SendState.RETRY


def test_reopen_after_a_voided_approval_sends_and_schedules_nothing(rig):
    reopen_failed(rig)
    with rig.backend.session(rig.t) as s:
        s.write(void_bundle_approvals(s.get(Bundle, rig.shop.bundle.id), s.list(Variant, bundle_id=rig.shop.bundle.id),
                                      s.gate.get_approvals(rig.t, rig.shop.bundle.id), reason="edited",
                                      actor=rig.shop.human, clock=rig.backend.clock, audit=s.audit, signer=rig.signer))
    out = rig.publish()
    assert out.status is Status.BLOCKED and BlockReason.APPROVAL_VOIDED.value in out.reasons
    assert len(rig.poster.calls) == 1 and variant(rig).state is not VariantState.SCHEDULED


def test_a_state_move_the_machine_refuses_rolls_the_whole_record_back(rig, monkeypatch):
    from thallo.publish.store import PublishStore

    real = PublishStore.move_variant

    def refuse_published(self, variant_id, target):
        return False if target is VariantState.PUBLISHED else real(self, variant_id, target)
    monkeypatch.setattr(PublishStore, "move_variant", refuse_published)
    with pytest.raises(Exception, match="cannot move"):
        rig.publish()
    (pub,) = rows(rig, Publication)
    assert pub.external_id is None and send_row(rig).state is SendState.SENDING  # nothing half-written
    assert events(rig, "publication.sent") == []
