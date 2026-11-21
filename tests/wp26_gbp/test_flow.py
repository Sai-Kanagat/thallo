"""A GBP post and a photo update go through the real gate and the real publisher to the manual pack
(no API access), on memory and Postgres. The GBP provider is in the chain but unconfigured, so the
chain falls through to the manual floor, which is what a tenant without API access gets."""
# ruff: noqa: F811  (backend, signer and pg_urls are fixtures imported from the wave 1 flow)
from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager

import pytest

from tests.integration.conftest import pg_urls  # noqa: F401
from tests.integration.test_wave1_flow import _id, backend, seed, signer  # noqa: F401
from thallo.connections import InMemoryConnectionStore, ProviderRegistry
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts.enums import (
    ApprovalMethod,
    Capability,
    Channel,
    CheckLayer,
    CheckVerdict,
    MediaKind,
    MediaProvenance,
)
from thallo.contracts.models import ChannelAccount, CheckResult, Connection, Media
from thallo.contracts.states import BundleState, VariantState
from thallo.extensions.gbp import PostRefused, build_pack, build_photo_update, build_post
from thallo.gate import approve_bundle_from_repo, content_hashes, load_bundle
from thallo.gate.canonical import build_snapshot
from thallo.gate.checks import PHOTO_ATTESTATION, PHOTO_CHECK
from thallo.providers.publishing import google_business_profile as gbp
from thallo.publish import PgPublishStore, Publisher, PublishSession, PublishStore, Status, WaitingOn
from thallo.truth import library

PHOTO = b"\x89PNG client shop front"


def rig(b, shop, signer):
    clock = b.clock
    registry = ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                                  connections=InMemoryConnectionStore(), environ={})
    t = shop.tenant.id
    registry.add(t, Capability.PUBLISHING, gbp.NAME, settings={"account_id": "111", "location_id": "222"})
    registry.ensure_manual(t, Capability.PUBLISHING)

    @contextmanager
    def sessions(tenant_id):
        with b.session(tenant_id) as s:
            store = (PgPublishStore(s.repo, tenant_id, clock) if b.name == "postgres"
                     else PublishStore(s, tenant_id, clock))
            yield PublishSession(gate=s.gate, store=store)

    with b.session(t) as s:
        account = s.insert(ChannelAccount(id=_id(b), created_at=clock.now(), tenant_id=t,
                                          channel=Channel.GOOGLE_BUSINESS_PROFILE,
                                          handle=f"shop-{secrets.token_hex(2)}"))
        if b.name == "postgres":
            have = {r.id for r in s.list(Connection)}
            for row in registry.connections.list_for_tenant(t):
                if row.id not in have:
                    s.insert(row)
    return Publisher(registry=registry, sessions=sessions, clock=clock, signer=signer), account


def land(b, shop, piece, *, lint_media=()):
    """Insert the piece, put it in review, add the remaining check rows, approve it as a person."""
    t = shop.tenant.id
    bundle = piece.bundle.model_copy(update={"state": BundleState.IN_REVIEW})
    variant = piece.variant.model_copy(update={"state": VariantState.REVIEW})
    with b.session(t) as s:
        for row in [piece.idea, piece.brief, piece.asset, bundle, variant, *piece.variant_claims, *piece.variant_media]:
            s.insert(row)
        sha = content_hashes(s.gate, t, bundle.id)[variant.id]
        for row in piece.check_results:
            assert row.content_sha256 == sha  # the outcome's rows are bound to the hash the gate computes
            s.insert(row)
        flags = [r.id for r in piece.check_results if r.verdict.value == "flag"]
        for m in lint_media:  # what the WP-07 photo check writes for a client photo
            for name, verdict in ((PHOTO_CHECK, CheckVerdict.PASS), (PHOTO_ATTESTATION, CheckVerdict.FLAG)):
                row = s.insert(CheckResult(id=_id(b), created_at=b.clock.now(), tenant_id=t, variant_id=variant.id,
                                           layer=CheckLayer.CODE, check_name=name, verdict=verdict,
                                           content_sha256=sha, media_sha256=m.sha256))
                if verdict is CheckVerdict.FLAG:
                    flags.append(row.id)
    b.clock.advance(seconds=30)
    with b.session(t) as s:
        content = load_bundle(s.gate, t, s.gate.get_bundle(t, bundle.id))
        snap = build_snapshot(content, lambda u: s.gate.resolve_link(t, u), frozenset(s.gate.non_link_allowlist(t)))
        change = approve_bundle_from_repo(
            s.gate, tenant_id=t, bundle_id=bundle.id, actor=shop.human, verifier=shop.verifier,
            shown_hash=snap.hash.root, method=ApprovalMethod.PER_BUNDLE, seconds_on_screen=20.0, clock=b.clock,
            audit=s.audit, signer=signer_of(b), acknowledged_flag_ids=flags)
        s.write(change)
    return bundle, variant, change.new_approval


_SIGNERS: dict[int, object] = {}


def signer_of(b):
    return _SIGNERS[id(b)]


def test_post_and_photo_update_reach_the_manual_pack_through_the_gate(backend, signer):
    _SIGNERS[id(backend)] = signer
    shop = seed(backend, "Gbp")
    t = shop.tenant.id
    publisher, account = rig(backend, shop, signer)
    with backend.session(t) as s:
        claims = library(s.claims, t)
        resolver = lambda u: s.gate.resolve_link(t, u)  # noqa: E731
        non_links = frozenset(s.gate.non_link_allowlist(t))
        post = build_post(tenant=shop.tenant, text="Nuova bici in negozio: {{C-0001}} di autonomia.",
                          title="New bike", claims=claims, clock=backend.clock, resolver=resolver,
                          non_links=non_links)
        uri = backend.blobs.write(t, "front.png", PHOTO)
        photo = s.insert(Media(id=_id(backend), created_at=backend.clock.now(), tenant_id=t, kind=MediaKind.IMAGE,
                               sha256=hashlib.sha256(PHOTO).hexdigest(), mime="image/png", size_bytes=len(PHOTO),
                               storage_uri=uri, provenance=MediaProvenance.CLIENT_PHOTO, alt_text="Shop front"))
        update = build_photo_update(tenant=shop.tenant, photos=[photo], claims=claims, clock=backend.clock,
                                    resolver=resolver, non_links=non_links, caption="Il negozio oggi.")
    assert post.variant.channel is Channel.GOOGLE_BUSINESS_PROFILE and post.variant.task_type == "gbp_post"

    packs = []
    for piece, media in ((post, ()), (update, (photo,))):
        bundle, variant, approval = land(backend, shop, piece, lint_media=media)
        backend.clock.advance(seconds=5)
        out = publisher.publish(tenant_id=t, bundle_id=bundle.id, variant_id=variant.id, approval_id=approval.id,
                                channel_account_id=account.id)
        assert out.status is Status.MANUAL_PENDING and out.waiting_on is WaitingOn.PERSON, out
        with backend.session(t) as s:
            store = (PgPublishStore(s.repo, t, backend.clock) if backend.name == "postgres"
                     else PublishStore(s, t, backend.clock))
            pub = store.publication_for_variant(variant.id)
            assert pub is not None
        packs.append({"channel": "google_business_profile", "text": variant.rendered_text,
                      "media_sha256": [m.sha256 for m in media], "variant_id": variant.id})

    pack = build_pack(copy_packs=packs, drafts=[], items={}, proposals=[], decisions=[])
    assert len(pack.posts) == 2 and "{{" not in pack.posts[0]["text"]


def test_an_unapproved_post_never_reaches_the_pack(backend, signer):
    _SIGNERS[id(backend)] = signer
    shop = seed(backend, "Gbp2")
    t = shop.tenant.id
    publisher, account = rig(backend, shop, signer)
    with backend.session(t) as s:
        post = build_post(tenant=shop.tenant, text="Aperti anche il sabato.", title="Saturday",
                          claims=library(s.claims, t), clock=backend.clock,
                          resolver=lambda u: s.gate.resolve_link(t, u), non_links=frozenset())
        for row in post.rows():
            s.insert(row)
    out = publisher.publish(tenant_id=t, bundle_id=post.bundle.id, variant_id=post.variant.id,
                            approval_id=shop.flag_ids[0] if shop.flag_ids else post.variant.id,
                            channel_account_id=account.id)
    assert out.status is Status.BLOCKED


def test_generated_images_are_refused_for_the_profile(backend):
    shop = seed(backend, "Gbp3")
    with pytest.raises(PostRefused, match="client's own"):
        build_photo_update(tenant=shop.tenant, photos=[shop.media.model_copy(
            update={"provenance": MediaProvenance.GENERATED})], claims={}, clock=backend.clock,
            resolver=lambda u: None, non_links=frozenset())
