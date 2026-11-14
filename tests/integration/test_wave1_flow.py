"""Wave 1 end to end: store, truth, gate and the audit log working together.

Every test runs twice: in memory, and on Postgres as the per-run app role through the real
adapters (`thallo.store.adapters`). The happy path builds a tenant from nothing to an
allowed publish whose payload is exactly what was approved. Each test after it changes one thing
and shows publish is blocked. The last ones verify the audit chain end to end and that a gate
change is all or nothing.
"""
from __future__ import annotations

import hashlib
import secrets
import string
from dataclasses import dataclass
from datetime import timedelta

import pytest

from thallo.contracts.actors import HumanActor, SystemActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import (
    ApprovalMethod,
    ApprovalSubject,
    Channel,
    CheckVerdict,
    ClaimKind,
    FunnelStage,
    Language,
    MediaKind,
    MediaProvenance,
    SourceKind,
    UserRole,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Approval,
    AppUser,
    Asset,
    AuditEntry,
    Brief,
    Bundle,
    CheckResult,
    Claim,
    Idea,
    Identity,
    Media,
    Operator,
    Source,
    Tenant,
    TrackedLink,
    Variant,
    VariantClaim,
    VariantMedia,
)
from thallo.contracts.states import BundleState, ClaimState, VariantState
from thallo.gate import (
    ApprovalRefused,
    BlockReason,
    approve_bundle_from_repo,
    check_publish,
    content_hashes,
    load_bundle,
    void_bundle_approvals,
)
from thallo.gate.approvals import ACTION_GRANTED, ACTION_VOIDED, AUDIT_SUBJECT
from thallo.gate.canonical import build_snapshot
from thallo.gate.signing import ENV_KEY, GateSigner
from thallo.store.adapters import ClaimWriteRefused, LocalBlobStore, TenantMismatch
from thallo.trust.audit import AuditConflict, verify_chain
from thallo.truth import build, change_claim, library, new_candidate, to_check_results, verify_claim

from .backends import REDIRECT, MemoryBackend, PgBackend

TEXT = "Fino a {{C-0001}} con una carica, il top. Scopri di più: " + REDIRECT + "SLUG"
DESTINATION = "https://bici-demo.example/offerta"
IMAGE = b"\x89PNG wave one hero image"


class FakeVerifier:
    """Stands in for the API layer's SessionVerifier: only the live session counts as human."""

    def __init__(self, live: str) -> None:
        self.live = live

    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id == self.live


@pytest.fixture(params=["memory", "postgres"])
def backend(request, tmp_path):
    clock = FakeClock(FAKE_EPOCH)
    blobs = LocalBlobStore(tmp_path / "blobs")
    if request.param == "memory":
        return MemoryBackend(clock, blobs)
    return PgBackend(request.getfixturevalue("pg_urls"), clock, blobs)


@pytest.fixture
def signer() -> GateSigner:
    return GateSigner(secrets.token_bytes(32))  # generated per test, never stored


@dataclass
class Shop:
    tenant: Tenant
    founder: AppUser
    human: HumanActor
    verifier: FakeVerifier
    claim: Claim
    bundle: Bundle
    variant: Variant
    media: Media
    blob_name: str
    flag_ids: list[str]
    approval: Approval | None = None
    approved_payload: object = None


def _id(b) -> str:
    b.clock.advance(seconds=1)
    return new_id(b.clock)


def seed(b, name: str, *, checks: bool = True) -> Shop:
    """Operator, tenant, founder, source, verified claim, compiled copy with one flag, the check
    rows (unless `checks` is False), an asset, a bundle in review with one variant, one image on the
    blob store, a tracked link."""
    clock = b.clock
    now = clock.now()
    operator = Operator(id=_id(b), created_at=now, name=f"op {name}")
    tenant = Tenant(id=_id(b), created_at=now, operator_id=operator.id, name=name, currency="EUR",
                    slug=f"{name.lower()}-{secrets.token_hex(3)}", default_language=Language.IT, region_code="IT")
    identity = Identity(id=_id(b), created_at=now, email=f"founder-{secrets.token_hex(3)}@example.test",
                        display_name="Founder")
    b.provision(operator, tenant, identity)
    t = tenant.id
    session = f"live-{secrets.token_hex(4)}"
    with b.session(t) as s:
        founder = s.insert(AppUser(id=_id(b), created_at=now, tenant_id=t, identity_id=identity.id,
                                   display_name="Founder", role=UserRole.FOUNDER))
        human = HumanActor(id=founder.id, tenant_id=t, session_id=session)
        verifier = FakeVerifier(session)
        source = s.insert(Source(id=_id(b), created_at=now, tenant_id=t, kind=SourceKind.DOCUMENT,
                                 title="Spec sheet", captured_at=now))
        candidate, _ = new_candidate(tenant_id=t, code="C-0001", kind=ClaimKind.NUMBER, value="120", unit="km",
                                     clock=clock, actor=SystemActor(id="extractor"), source_id=source.id,
                                     evidence="spec sheet page 2: range 120 km",
                                     languages={"it": "120 km di autonomia"})
        s.claims.save(candidate)
        claim, _ = verify_claim(candidate, human, verifier=verifier, clock=clock, repo=s.claims)
        s.claims.save(claim)
        assert s.claims.latest(t, "C-0001").state is ClaimState.VERIFIED

        slug = "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
        built = build(TEXT.replace("SLUG", slug), tenant_id=t, language=Language.IT, claims=library(s.claims, t),
                      clock=clock, default_language=Language.IT)
        assert len(built.flags) == 1  # "il top": a flag word, for the approver ("una" counts nothing now)

        idea = s.insert(Idea(id=_id(b), created_at=now, tenant_id=t, stage=FunnelStage.CONSIDERATION,
                             title="Range", rationale="Riders ask about range"))
        brief = s.insert(Brief(id=_id(b), created_at=now, tenant_id=t, idea_id=idea.id, audience="Commuters",
                               job_to_be_done="Get to work", message="Long range", call_to_action="Prenota",
                               language=Language.IT))
        asset = s.insert(Asset(id=_id(b), created_at=now, tenant_id=t, brief_id=brief.id,
                               master_text=built.compiled.text))
        bundle = s.insert(Bundle(id=_id(b), created_at=now, tenant_id=t, asset_id=asset.id,
                                 state=BundleState.IN_REVIEW, rule_version=1))
        variant = s.insert(Variant(id=_id(b), created_at=now, tenant_id=t, bundle_id=bundle.id,
                                   channel=Channel.INSTAGRAM, language=Language.IT, state=VariantState.REVIEW,
                                   text=built.compiled.text, rendered_text=built.filled.text,
                                   task_type="offer_post"))
        for claim_id in built.compiled.claim_ids:
            s.insert(VariantClaim(id=_id(b), created_at=now, tenant_id=t, variant_id=variant.id, claim_id=claim_id))
        blob_name = "hero.png"
        uri = b.blobs.write(t, blob_name, IMAGE)
        media = s.insert(Media(id=_id(b), created_at=now, tenant_id=t, kind=MediaKind.IMAGE,
                               sha256=hashlib.sha256(IMAGE).hexdigest(), mime="image/png", size_bytes=len(IMAGE),
                               storage_uri=uri, provenance=MediaProvenance.CLIENT_PHOTO, alt_text="Bici rossa"))
        s.insert(VariantMedia(id=_id(b), created_at=now, tenant_id=t, variant_id=variant.id, media_id=media.id))
        s.insert(TrackedLink(id=_id(b), created_at=now, tenant_id=t, variant_id=variant.id, slug=slug,
                             destination=DESTINATION))
        # The code checks run on the finished variant, bound to the hash the gate computes for it.
        sha = content_hashes(s.gate, t, bundle.id)[variant.id]
        rows = to_check_results(built.compiled, variant_id=variant.id, content_sha256=sha, clock=clock)
        flag_ids: list[str] = []
        if checks:
            for row in rows:
                s.insert(row)
            flag_ids = [r.id for r in rows if r.verdict is CheckVerdict.FLAG]
            assert len(flag_ids) == 1
    return Shop(tenant=tenant, founder=founder, human=human, verifier=verifier, claim=claim, bundle=bundle,
                variant=variant, media=media, blob_name=blob_name, flag_ids=flag_ids)


def approve(b, shop: Shop, signer: GateSigner, *, acknowledged=None) -> Approval:
    t = shop.tenant.id
    b.clock.advance(seconds=30)
    with b.session(t) as s:
        bundle = s.gate.get_bundle(t, shop.bundle.id)
        content = load_bundle(s.gate, t, bundle)
        snapshot = build_snapshot(content, lambda url: s.gate.resolve_link(t, url),
                                  frozenset(s.gate.non_link_allowlist(t)))  # what the review card shows
        change = approve_bundle_from_repo(
            s.gate, tenant_id=t, bundle_id=bundle.id, actor=shop.human, verifier=shop.verifier,
            shown_hash=snapshot.hash.root, method=ApprovalMethod.PER_BUNDLE, seconds_on_screen=41.5,
            clock=b.clock, audit=s.audit, signer=signer,
            acknowledged_flag_ids=shop.flag_ids if acknowledged is None else acknowledged)
        s.write(change)
    shop.approval = change.new_approval
    shop.approved_payload = snapshot.payload(shop.variant.id)
    return change.new_approval


def publish(b, shop: Shop, signer: GateSigner | None, *, tenant_id=None, approval_id=None, **ids):
    t = tenant_id or shop.tenant.id
    b.clock.advance(seconds=5)
    with b.session(t) as s:
        return check_publish(s.gate, tenant_id=ids.get("as_tenant", t), bundle_id=ids.get("bundle_id", shop.bundle.id),
                             variant_id=ids.get("variant_id", shop.variant.id),
                             approval_id=approval_id or shop.approval.id, clock=b.clock, signer=signer)


@pytest.fixture
def shop(backend, signer) -> Shop:
    shop = seed(backend, "Alpha")
    approve(backend, shop, signer)
    return shop


# ---------------------------------------------------------------- happy path

def test_happy_path_publish_is_allowed_and_sends_what_was_approved(backend, shop, signer):
    decision = publish(backend, shop, signer)
    assert decision.allowed, decision.blocks
    assert decision.payload == shop.approved_payload
    assert decision.content_hash == shop.approval.content_hash
    assert decision.payload.text == shop.variant.rendered_text
    assert "120 km di autonomia" in decision.payload.text
    assert decision.payload.media_sha256 == (hashlib.sha256(IMAGE).hexdigest(),)
    with backend.session(shop.tenant.id) as s:
        bundle = s.get(Bundle, shop.bundle.id)
        assert bundle.state is BundleState.APPROVED and bundle.root_hash == shop.approval.content_hash
        assert s.get(Variant, shop.variant.id).state is VariantState.APPROVED
        stored = s.get(Approval, shop.approval.id)
        assert stored == shop.approval  # the row round-trips exactly, times in UTC
        assert stored.acknowledged_flag_ids == shop.flag_ids


# ---------------------------------------------------------------- one change, one block

def test_text_change_blocks(backend, shop, signer):
    with backend.session(shop.tenant.id) as s:
        s.update(Variant, shop.variant.id, rendered_text=shop.variant.rendered_text.replace("Fino", "Oltre"))
    decision = publish(backend, shop, signer)
    assert not decision.allowed and BlockReason.CONTENT_CHANGED in decision.reasons


def test_media_byte_change_at_the_same_path_blocks(backend, shop, signer):
    uri = backend.blobs.write(shop.tenant.id, shop.blob_name, IMAGE + b" retouched")
    assert uri == shop.media.storage_uri
    decision = publish(backend, shop, signer)
    assert not decision.allowed
    assert {BlockReason.MEDIA_BYTES_MISMATCH, BlockReason.CONTENT_CHANGED} <= set(decision.reasons)


def test_new_claim_version_blocks_and_never_rewrites_the_old_one(backend, shop, signer):
    t = shop.tenant.id
    with backend.session(t) as s:
        current = s.claims.latest(t, "C-0001")
        change = change_claim(current, actor=shop.human, verifier=shop.verifier, clock=backend.clock,
                              repo=s.claims, value="130",
                              languages={"it": "130 km di autonomia"})
        assert change.voided_variant_ids == (shop.variant.id,)
        assert change.voided_approval_ids == (shop.approval.id,)
        s.claims.save(change.previous)
        s.claims.save(change.current)
        history = s.claims.history(t, "C-0001")
        assert [(c.version, c.value, c.state) for c in history] == [
            (1, "120", ClaimState.VOIDED), (2, "130", ClaimState.CANDIDATE)]
        with pytest.raises(ClaimWriteRefused):  # a stored version is never rewritten
            s.claims.save(history[0].model_copy(update={"value": "999"}))
    decision = publish(backend, shop, signer)
    assert not decision.allowed and BlockReason.CLAIM_NOT_VERIFIED in decision.reasons


def test_void_blocks(backend, shop, signer):
    t = shop.tenant.id
    with backend.session(t) as s:
        change = void_bundle_approvals(s.get(Bundle, shop.bundle.id), s.list(Variant, bundle_id=shop.bundle.id),
                                       s.gate.get_approvals(t, shop.bundle.id), reason="caption edited",
                                       actor=shop.human, clock=backend.clock, audit=s.audit, signer=signer)
        s.write(change)
    decision = publish(backend, shop, signer)
    assert not decision.allowed
    assert {BlockReason.APPROVAL_VOIDED, BlockReason.BUNDLE_NOT_APPROVED} <= set(decision.reasons)


def test_unacknowledged_flag_refuses_the_grant(backend, signer):
    shop = seed(backend, "Gamma")
    with pytest.raises(ApprovalRefused, match="open flag"):
        approve(backend, shop, signer, acknowledged=[])
    with backend.session(shop.tenant.id) as s:  # nothing was written
        assert s.list(Approval) == [] and s.list(AuditEntry) == []
        assert s.get(Bundle, shop.bundle.id).state is BundleState.IN_REVIEW


def add_check(b, shop: Shop, verdict: CheckVerdict, *, attempt: int, like_flag: bool = True) -> str:
    """Another check_result row on the shop's variant; `like_flag` reuses the seeded flag's
    layer and check name, so it shares that check's key."""
    t = shop.tenant.id
    with b.session(t) as s:
        seeded = s.get(CheckResult, shop.flag_ids[0])
        row = s.insert(CheckResult(
            id=_id(b), created_at=b.clock.now(), tenant_id=t, variant_id=shop.variant.id, layer=seeded.layer,
            check_name=seeded.check_name if like_flag else "truth.compile.other", verdict=verdict,
            reason=f"{verdict.value} at attempt {attempt}", attempt=attempt, content_sha256=seeded.content_sha256))
    return row.id


def test_two_same_name_flags_at_one_attempt_each_need_acknowledging(backend, signer):
    shop = seed(backend, "Eta")
    second = add_check(backend, shop, CheckVerdict.FLAG, attempt=1)
    with pytest.raises(ApprovalRefused, match="1 open flag"):
        approve(backend, shop, signer, acknowledged=shop.flag_ids)
    with pytest.raises(ApprovalRefused, match="1 open flag"):
        approve(backend, shop, signer, acknowledged=[second])
    granted = approve(backend, shop, signer, acknowledged=[*shop.flag_ids, second])
    assert sorted(granted.acknowledged_flag_ids) == sorted([*shop.flag_ids, second])


def test_a_fail_at_the_latest_attempt_refuses_the_grant(backend, signer):
    shop = seed(backend, "Theta")
    fail = add_check(backend, shop, CheckVerdict.FAIL, attempt=1, like_flag=False)
    with pytest.raises(ApprovalRefused, match="FAIL"):
        approve(backend, shop, signer)  # every flag acknowledged
    with pytest.raises(ApprovalRefused):
        approve(backend, shop, signer, acknowledged=[*shop.flag_ids, fail])
    with backend.session(shop.tenant.id) as s:
        assert s.list(Approval) == [] and s.get(Bundle, shop.bundle.id).state is BundleState.IN_REVIEW


def test_an_older_fail_superseded_by_a_pass_does_not_block(backend, signer):
    shop = seed(backend, "Iota")
    add_check(backend, shop, CheckVerdict.FAIL, attempt=1, like_flag=False)
    add_check(backend, shop, CheckVerdict.PASS, attempt=2, like_flag=False)
    assert approve(backend, shop, signer).acknowledged_flag_ids == shop.flag_ids


def test_pass_and_fail_of_one_check_at_the_latest_attempt_blocks(backend, signer):
    shop = seed(backend, "Kappa")
    add_check(backend, shop, CheckVerdict.FAIL, attempt=2, like_flag=False)
    add_check(backend, shop, CheckVerdict.PASS, attempt=2, like_flag=False)  # newer row, same attempt
    with pytest.raises(ApprovalRefused, match="FAIL"):
        approve(backend, shop, signer)


def test_missing_signing_key_blocks(backend, shop, monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    decision = publish(backend, shop, None)
    assert not decision.allowed and decision.reasons == (BlockReason.GATE_KEY_MISSING,)


def test_a_different_signing_key_blocks(backend, shop):
    decision = publish(backend, shop, GateSigner(secrets.token_bytes(32)))
    assert not decision.allowed and BlockReason.NO_GRANT_RECORD in decision.reasons


def test_system_pause_blocks(backend, shop, signer):
    backend.set_paused(True)
    try:
        decision = publish(backend, shop, signer)
    finally:
        backend.set_paused(False)
    assert not decision.allowed and BlockReason.SYSTEM_PAUSED in decision.reasons
    assert publish(backend, shop, signer).allowed


def test_forged_approval_row_blocks(backend, shop, signer):
    """The app role can insert an approval row and even a well-linked audit entry. Neither carries
    the gate key's MAC, so publish refuses."""
    t = shop.tenant.id
    now = backend.clock.now()
    with backend.session(t) as s:
        forged = s.insert(Approval(id=_id(backend), created_at=now, tenant_id=t,
                                   subject_kind=ApprovalSubject.BUNDLE, subject_id=shop.bundle.id,
                                   content_hash=shop.approval.content_hash, approver_id=shop.founder.id,
                                   method=ApprovalMethod.PER_BUNDLE, approved_at=now,
                                   expires_at=now + timedelta(days=1), acknowledged_flag_ids=shop.flag_ids))
    decision = publish(backend, shop, signer, approval_id=forged.id)
    assert not decision.allowed and BlockReason.NO_GRANT_RECORD in decision.reasons
    with backend.session(t) as s:
        s.audit.append(t, shop.human, ACTION_GRANTED, subject_kind=AUDIT_SUBJECT, subject_id=forged.id, payload={
            "bundle_id": shop.bundle.id, "content_hash": forged.content_hash, "method": "per_bundle",
            "approved_at": forged.approved_at.isoformat(), "expires_at": forged.expires_at.isoformat(),
            "canary": False, "acknowledged_flag_ids": shop.flag_ids, "mac": "0" * 64})
    decision = publish(backend, shop, signer, approval_id=forged.id)
    assert not decision.allowed and BlockReason.NO_GRANT_RECORD in decision.reasons
    assert publish(backend, shop, signer).allowed  # the real approval still stands


def test_tenant_b_cannot_use_tenant_a_approval(backend, shop, signer):
    other = seed(backend, "Beta")
    a, b = shop.tenant.id, other.tenant.id
    # B's own bundle with A's approval id: A's row is invisible to B.
    decision = publish(backend, other, signer, approval_id=shop.approval.id)
    assert not decision.allowed and BlockReason.NO_APPROVAL in decision.reasons
    # B asking for A's bundle inside B's scope: not found.
    decision = publish(backend, other, signer, approval_id=shop.approval.id, bundle_id=shop.bundle.id,
                       variant_id=shop.variant.id)
    assert not decision.allowed and decision.reasons == (BlockReason.NO_BUNDLE,)
    # A repository scoped to B asked to read for A: refused, and the gate turns it into a block.
    decision = publish(backend, other, signer, tenant_id=b, as_tenant=a, approval_id=shop.approval.id,
                       bundle_id=shop.bundle.id, variant_id=shop.variant.id)
    assert not decision.allowed and decision.reasons == (BlockReason.REPO_ERROR,)
    with backend.session(b) as s:
        assert s.get(Approval, shop.approval.id) is None
        with pytest.raises(TenantMismatch):
            s.gate.get_approval(a, shop.approval.id)
    assert publish(backend, shop, signer).allowed  # A is unaffected


# ---------------------------------------------------------------- the log and the transaction

def test_audit_chain_verifies_end_to_end(backend, shop, signer):
    """Grant, void, re-approve: every change is in the chain, in order, and the chain is intact."""
    t = shop.tenant.id
    first = shop.approval
    with backend.session(t) as s:
        s.write(void_bundle_approvals(s.get(Bundle, shop.bundle.id), s.list(Variant, bundle_id=shop.bundle.id),
                                      s.gate.get_approvals(t, shop.bundle.id), reason="caption edited",
                                      actor=shop.human, clock=backend.clock, audit=s.audit, signer=signer))
        s.update(Bundle, shop.bundle.id, state=BundleState.IN_REVIEW)  # back through checks to review
        s.update(Variant, shop.variant.id, state=VariantState.REVIEW)
    second = approve(backend, shop, signer)
    assert publish(backend, shop, signer).allowed
    assert not publish(backend, shop, signer, approval_id=first.id).allowed
    with backend.session(t) as s:
        report = s.audit.verify(t)
        entries = s.audit.store.entries(t)
        assert report.ok, report.problems
        assert [(e.seq, e.action, e.subject_id) for e in entries] == [
            (1, ACTION_GRANTED, first.id), (2, ACTION_VOIDED, first.id), (3, ACTION_GRANTED, second.id)]
        assert s.audit.head(t) == (3, entries[-1].entry_hash)
        assert all(e.actor_id == shop.founder.id for e in entries)
        tampered = list(entries)
        tampered[1] = tampered[1].model_copy(update={"payload": {**tampered[1].payload, "reason": "nothing"}})
        assert not verify_chain(tampered, tenant_id=t).ok


def test_a_stale_gate_change_conflicts_and_writes_nothing(backend, signer):
    """Two writers prepared the same next seq: the second change fails as a whole."""
    shop = seed(backend, "Delta")
    t = shop.tenant.id
    with backend.session(t) as s:
        content = load_bundle(s.gate, t, s.gate.get_bundle(t, shop.bundle.id))
        shown = build_snapshot(content, lambda url: s.gate.resolve_link(t, url), frozenset()).hash.root
        stale = approve_bundle_from_repo(
            s.gate, tenant_id=t, bundle_id=shop.bundle.id, actor=shop.human, verifier=shop.verifier,
            shown_hash=shown, method=ApprovalMethod.PER_BUNDLE, seconds_on_screen=12.0, clock=backend.clock,
            audit=s.audit, signer=signer, acknowledged_flag_ids=shop.flag_ids)
    with backend.session(t) as s:
        s.audit.append(t, shop.human, "note.added", subject_kind="bundle", subject_id=shop.bundle.id)
    with pytest.raises(AuditConflict), backend.session(t) as s:
        s.write(stale)
    with backend.session(t) as s:
        assert s.get(Approval, stale.new_approval.id) is None
        assert s.get(Bundle, shop.bundle.id).state is BundleState.IN_REVIEW
        assert s.get(Variant, shop.variant.id).state is VariantState.REVIEW
        assert [e.action for e in s.audit.store.entries(t)] == ["note.added"]
        assert s.audit.verify(t).ok


def test_postgres_app_role_cannot_rewrite_the_audit_log(backend, shop):
    if not isinstance(backend, PgBackend):
        pytest.skip("database privileges: Postgres only")
    import psycopg

    for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), backend.session(shop.tenant.id) as s:
            s.repo.execute(statement)
