"""The copy outcome written to a real, migrated Postgres as the app role (row-level security on):
every row satisfies the schema, the foreign keys and the tenant policy. Skipped without
DATABASE_URL (scripts/ci.sh sets it)."""
from __future__ import annotations

import hashlib
import os
import secrets
import sys
from pathlib import Path

import pytest

from thallo.contracts.actors import HumanActor, SystemActor
from thallo.contracts.enums import (
    Capability,
    Channel,
    CheckVerdict,
    ClaimKind,
    FunnelStage,
    Language,
    MediaKind,
    MediaProvenance,
    TextRole,
    UserRole,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    AppUser,
    Asset,
    Brief,
    CheckResult,
    CostEntry,
    Idea,
    Identity,
    Media,
    Operator,
    Tenant,
    Variant,
    VariantMedia,
)
from thallo.copy import CopyStatus, produce_copy, write_outcome
from thallo.gate import content_hashes, content_types, load_bundle, review_checks
from thallo.gate.canonical import build_snapshot
from thallo.store.adapters import LocalBlobStore
from thallo.store.adapters.claims import PgClaimRepository
from thallo.store.adapters.gate import PgGateRepository
from thallo.truth import library, new_candidate, to_check_results, verify_claim

from .test_copy import CLEAN, DIGIT, SOURCE, Script, Verifier, make_registry

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL")


@pytest.fixture(scope="module")
def pg_urls():
    if not URL:
        pytest.skip("DATABASE_URL unset: the Postgres test runs under scripts/ci.sh")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_wp06_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(URL)
    params["dbname"] = name
    admin_url = make_conninfo(**params)
    migrate(admin_url)
    password = secrets.token_hex(16)
    role = f"cortex_app_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE cortex_app "
                     "NOSUPERUSER NOBYPASSRLS INHERIT")
    params.update(user=role, password=password)
    try:
        yield {"admin": admin_url, "app": make_conninfo(**params)}
    finally:
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"DROP ROLE IF EXISTS {role}")


def gate_review(gate, t: str, bundle_id: str):
    """What approval decides about checks, read back from storage exactly as approval loads it."""
    content = load_bundle(gate, t, gate.get_bundle(t, bundle_id))
    snap = build_snapshot(content, lambda url: gate.resolve_link(t, url), frozenset(gate.non_link_allowlist(t)))
    hashes = dict(snap.hash.variants)
    rows = [r for vid, sha in hashes.items() for r in gate.get_check_results(t, vid, sha)]
    return review_checks(rows, tenant_id=t, hashes=hashes,
                         types={vid: content_types(snap.payload(vid)) for vid in hashes})


def test_outcome_round_trips_through_postgres(pg_urls, clock, vault, tmp_path):
    import psycopg

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
        idea = repo.insert(Idea(id=new_id(clock), created_at=now, tenant_id=t, stage=FunnelStage.CONSIDERATION,
                                title="Bread", rationale="weekend orders"))
        brief = repo.insert(Brief(id=new_id(clock), created_at=now, tenant_id=t, idea_id=idea.id,
                                  audience="neighbours", job_to_be_done="buy bread", message="fresh daily",
                                  call_to_action="order", allowed_claim_ids=[claim.id], language=Language.EN))
        lib = library(claims, t)

    script = Script(DIGIT, CLEAN)
    reg = make_registry(clock, vault, script, configure=False)
    reg.add(t, Capability.TEXT, "claude", role=TextRole.DRAFT, model="claude-sonnet-5-5", secret="fake-key-for-tests",
            settings={"input_usd_per_mtok": "2", "output_usd_per_mtok": "10"})
    blobs = LocalBlobStore(tmp_path / "blobs")
    with db.tenant(t) as repo:  # the repository's own resolver and allowlist, as approval uses them
        gate = PgGateRepository(repo, blobs)
        out = produce_copy(registry=reg, tenant=tenant, brief=brief, claims=lib, sources=[SOURCE],
                           channel=Channel.INSTAGRAM, rule_version=1, clock=clock, candidates=1,
                           resolver=lambda url: gate.resolve_link(t, url), non_links=gate.non_link_allowlist(t))
    assert out.status is CopyStatus.READY_FOR_CHECKS

    with db.tenant(t) as repo:
        write_outcome(repo, out)
    with db.tenant(t) as repo:
        asset = repo.get(Asset, out.asset.id)
        assert asset.selection_reason == out.asset.selection_reason and len(asset.candidates) == 1
        assert repo.get(Variant, out.variant.id).rendered_text == out.variant.rendered_text
        rows = repo.list(CheckResult, variant_id=out.variant.id)
        assert {(r.attempt, r.check_name, r.verdict) for r in rows} >= {
            (1, "truth.compile.digit", CheckVerdict.FAIL), (2, "truth.compile.digit", CheckVerdict.PASS)}
        assert len(repo.list(CostEntry)) == 2
        # Ruling 3: every row carries the hash the gate computes from storage, nothing guessed.
        gate = PgGateRepository(repo, blobs)
        stored = content_hashes(gate, t, out.bundle.id)[out.variant.id]
        assert {r.content_sha256 for r in rows} == {stored}
        review = gate_review(gate, t, out.bundle.id)
        assert not review.unchecked and not review.failing  # the repaired attempt decides every key

        # Ruling 1: the copy rows checked text only. Once media is attached the hash changes and
        # they cover nothing; even rows for the new hash need a design-lint row besides truth.
        data = b"\x89PNG\r\n\x1a\nimage"
        uri = blobs.write(t, "hero.png", data)
        media = repo.insert(Media(id=new_id(clock), created_at=clock.now(), tenant_id=t, kind=MediaKind.IMAGE,
                                  sha256=hashlib.sha256(data).hexdigest(), mime="image/png", size_bytes=len(data),
                                  storage_uri=uri, provenance=MediaProvenance.CLIENT_PHOTO, alt_text="Bread"))
        repo.insert(VariantMedia(id=new_id(clock), created_at=clock.now(), tenant_id=t, variant_id=out.variant.id,
                                 media_id=media.id))
        review = gate_review(gate, t, out.bundle.id)
        assert review.unchecked == (out.variant.id,)
        assert [k.value for k in review.missing[out.variant.id]] == ["text", f"media.client_photo.{media.sha256[:12]}"]
        with_media = content_hashes(gate, t, out.bundle.id)[out.variant.id]
        assert with_media != stored
        winner = out.candidates[0].built
        for r in to_check_results(winner.compiled, variant_id=out.variant.id, content_sha256=with_media,
                                  clock=clock, attempt=3):
            repo.insert(r)
        review = gate_review(gate, t, out.bundle.id)
        assert [k.value for k in review.missing[out.variant.id]] == [f"media.client_photo.{media.sha256[:12]}"]
