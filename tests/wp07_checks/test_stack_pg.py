"""The stack against a real, migrated Postgres as the app role (row-level security on): the finished
piece is loaded exactly as approval loads it, the rows go through `write_stack`, and the gate's own
review read back from storage decides. Skipped without DATABASE_URL (scripts/ci.sh sets it)."""
from __future__ import annotations

import hashlib
import secrets

import psycopg  # noqa: F401 - the db extra; the fixture skips without DATABASE_URL first

from tests.wp06_text.test_copy_pg import gate_review, pg_urls  # noqa: F401 - the shared per-run database
from thallo.checks import Context, FinishedPiece, StackStatus, run_stack, write_stack
from thallo.contracts.actors import HumanActor, SystemActor
from thallo.contracts.enums import (
    Channel,
    CheckLayer,
    CheckVerdict,
    ClaimKind,
    FunnelStage,
    Language,
    MediaKind,
    MediaProvenance,
    UserRole,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    AppUser,
    Asset,
    Brief,
    Bundle,
    CheckResult,
    CostEntry,
    Idea,
    Identity,
    Media,
    Operator,
    Tenant,
    Variant,
    VariantClaim,
    VariantMedia,
)
from thallo.contracts.states import VariantState
from thallo.gate import load_bundle
from thallo.gate.checks import PHOTO_ATTESTATION
from thallo.store.adapters import LocalBlobStore
from thallo.store.adapters.claims import PgClaimRepository
from thallo.store.adapters.gate import PgGateRepository
from thallo.truth import build, library, new_candidate, verify_claim

from .conftest import CLEAN, PASSING_LINT, SHARP_PHOTO, SOURCE, TEMPLATE_PNG, Vendors, Verifier, make_registry


def test_stack_rows_cover_the_finished_piece_in_postgres(pg_urls, clock, vault, tmp_path):  # noqa: F811
    from thallo.store import Database

    db = Database(pg_urls["app"], clock, admin_url=pg_urls["admin"])
    now = clock.now()
    op = db.create_operator(Operator(id=new_id(clock), created_at=now, name="op"))
    tenant = db.create_tenant(Tenant(id=new_id(clock), created_at=now, operator_id=op.id, name="Bakery",
                                     slug=f"bakery-{secrets.token_hex(3)}", currency="EUR",
                                     default_language=Language.EN, region_code="IT"))
    identity = Identity(id=new_id(clock), created_at=now, email=f"f-{secrets.token_hex(3)}@example.test",
                        display_name="Founder")
    with psycopg.connect(pg_urls["admin"]) as conn:
        conn.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                     (identity.id, identity.created_at, identity.email, identity.display_name))
    t = tenant.id
    blobs = LocalBlobStore(tmp_path / "blobs")
    with db.tenant(t) as repo:
        founder = repo.insert(AppUser(id=new_id(clock), created_at=now, tenant_id=t, identity_id=identity.id,
                                      display_name="Founder", role=UserRole.FOUNDER))
        claims = PgClaimRepository(repo)
        cand, _ = new_candidate(tenant_id=t, code="C-0001", kind=ClaimKind.PRICE, value="12", unit="EUR",
                                clock=clock, actor=SystemActor(id="extractor"), evidence="price list")
        claims.save(cand)
        claim, _ = verify_claim(cand, HumanActor(id=founder.id, tenant_id=t, session_id="live"),
                                verifier=Verifier(), clock=clock, repo=claims)
        claims.save(claim)
        lib = library(claims, t)
        idea = repo.insert(Idea(id=new_id(clock), created_at=now, tenant_id=t, stage=FunnelStage.CONSIDERATION,
                                title="Bread", rationale="weekend orders"))
        brief = repo.insert(Brief(id=new_id(clock), created_at=now, tenant_id=t, idea_id=idea.id,
                                  audience="neighbours", job_to_be_done="buy bread", message="fresh daily",
                                  call_to_action="order", allowed_claim_ids=[claim.id], language=Language.EN))
        built = build(CLEAN, tenant_id=t, language=Language.EN, claims=lib, clock=clock,
                      default_language=Language.EN)
        asset = repo.insert(Asset(id=new_id(clock), created_at=now, tenant_id=t, brief_id=brief.id,
                                  master_text=CLEAN))
        bundle = repo.insert(Bundle(id=new_id(clock), created_at=now, tenant_id=t, asset_id=asset.id,
                                    rule_version=1))
        variant = repo.insert(Variant(id=new_id(clock), created_at=now, tenant_id=t, bundle_id=bundle.id,
                                      channel=Channel.INSTAGRAM, language=Language.EN, state=VariantState.CHECKING,
                                      text=CLEAN, rendered_text=built.filled.text, task_type="post",
                                      model_id="claude-sonnet-5-5"))
        repo.insert(VariantClaim(id=new_id(clock), created_at=now, tenant_id=t, variant_id=variant.id,
                                 claim_id=claim.id))
        for name, data, prov in (("render.png", TEMPLATE_PNG, MediaProvenance.TEMPLATE_RENDER),
                                 ("photo.png", SHARP_PHOTO, MediaProvenance.CLIENT_PHOTO)):
            uri = blobs.write(t, name, data)
            media = repo.insert(Media(id=new_id(clock), created_at=now, tenant_id=t, kind=MediaKind.IMAGE,
                                      sha256=hashlib.sha256(data).hexdigest(), mime="image/png",
                                      size_bytes=len(data), storage_uri=uri, provenance=prov, alt_text="Bread"))
            repo.insert(VariantMedia(id=new_id(clock), created_at=now, tenant_id=t, variant_id=variant.id,
                                     media_id=media.id))

    vendors = Vendors()
    reg = make_registry(clock, vault, vendors, tenant_id=t)
    with db.tenant(t) as repo:
        gate = PgGateRepository(repo, blobs)
        content = load_bundle(gate, t, gate.get_bundle(t, bundle.id))
        vc = content.variant(variant.id)
        render_sha = hashlib.sha256(TEMPLATE_PNG).hexdigest()
        piece = FinishedPiece(content=vc, media_bytes={m.bytes_sha256: blobs.read(t, m.media.storage_uri)
                                                       for m in vc.media},
                              lint={render_sha: PASSING_LINT}, sources=(SOURCE,), brief=brief)
        ctx = Context(tenant=tenant, claims=lib, clock=clock, resolver=lambda url: gate.resolve_link(t, url),
                      non_links=gate.non_link_allowlist(t), registry=reg,
                      existing=repo.list(CheckResult, variant_id=variant.id))
        out = run_stack(piece, ctx)
    assert out.status is StackStatus.READY_FOR_REVIEW
    with db.tenant(t) as repo:
        write_stack(repo, out)

    with db.tenant(t) as repo:
        gate = PgGateRepository(repo, blobs)
        stored = repo.list(CheckResult, variant_id=variant.id)
        assert len(stored) == len(out.check_results)
        assert {r.media_sha256 for r in stored} >= {render_sha, hashlib.sha256(SHARP_PHOTO).hexdigest(), None}
        review = gate_review(gate, t, bundle.id)
        assert not review.unchecked and not review.failing
        att = [r for r in stored if r.check_name == PHOTO_ATTESTATION]
        assert len(att) == 1 and att[0].id in review.flags
        assert len(repo.list(CostEntry)) == len(vendors.grader.calls) + len(vendors.reader.calls) == 3
        assert {r.layer for r in stored} == {CheckLayer.CODE, CheckLayer.GRADER, CheckLayer.SECOND_READER}
        assert all(r.verdict is not CheckVerdict.FAIL for r in stored if r.layer is not CheckLayer.CODE)
