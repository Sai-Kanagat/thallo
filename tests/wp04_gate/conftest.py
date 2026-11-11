"""A small in-memory world for the gate tests: one tenant, one bundle in review, two variants,
media in a fake blob store addressed by path, and verified claims."""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from cortex_operator.contracts.actors import HumanActor
from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.enums import (
    ApprovalMethod,
    Channel,
    ClaimKind,
    Language,
    MediaKind,
    MediaProvenance,
)
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import Approval, Bundle, Claim, Media, Tenant, Variant, VariantMedia
from cortex_operator.contracts.states import BundleState, ClaimState, VariantState
from cortex_operator.gate import approve_bundle, load_bundle
from cortex_operator.trust.audit import AuditLog, InMemoryAuditStore

_counter = itertools.count(1)


def ident(clock: FakeClock) -> str:
    return new_id(clock, next(_counter).to_bytes(10, "big"))


class FakeVerifier:
    def __init__(self, live: set[str]) -> None:
        self.live = live

    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id in self.live


@dataclass
class World:
    clock: FakeClock
    tenant: Tenant
    bundle: Bundle
    variants: dict[str, Variant]
    media: dict[str, Media]
    variant_media: dict[str, list[VariantMedia]]
    claims: dict[str, Claim]
    variant_claims: dict[str, list[str]]
    blobs: dict[str, bytes]  # storage_uri to bytes
    approvals: dict[str, Approval] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    audit_store: InMemoryAuditStore = field(default_factory=InMemoryAuditStore)
    user_id: str = ""
    session: str = "live-session"

    # ----- GateRepository
    def get_tenant(self, tenant_id):
        return self.tenant if self.tenant.id == tenant_id else None

    def get_bundle(self, tenant_id, bundle_id):
        b = self.bundle
        return b if b.id == bundle_id and b.tenant_id == tenant_id else None

    def get_variants(self, tenant_id, bundle_id):
        return [v for v in self.variants.values() if v.bundle_id == bundle_id and v.tenant_id == tenant_id]

    def get_variant_media(self, tenant_id, variant_id):
        return [(vm, self.media[vm.media_id]) for vm in self.variant_media.get(variant_id, [])
                if vm.tenant_id == tenant_id]

    def read_media_bytes(self, tenant_id, media):
        return self.blobs[media.storage_uri]

    def get_variant_claims(self, tenant_id, variant_id):
        return [self.claims[c] for c in self.variant_claims.get(variant_id, [])]

    def get_approval(self, tenant_id, approval_id):
        a = self.approvals.get(approval_id)
        return a if a is not None and a.tenant_id == tenant_id else None

    def resolve_link(self, tenant_id, url):
        return self.links.get(url)

    # ----- helpers
    @property
    def audit(self) -> AuditLog:
        return AuditLog(self.audit_store, self.clock)

    def human(self) -> HumanActor:
        return HumanActor(id=self.user_id, tenant_id=self.tenant.id, session_id=self.session)

    def verifier(self) -> FakeVerifier:
        return FakeVerifier({self.session})

    def content(self):
        return load_bundle(self, self.tenant.id, self.bundle)

    def approve(self, **kw) -> Approval:
        """Approve as the verified human, store the approval and move the rows to approved."""
        from cortex_operator.gate import bundle_hash

        content = self.content()
        shown = bundle_hash(content, self.links.get).root
        granted = approve_bundle(content, actor=self.human(), verifier=self.verifier(), shown_hash=shown,
                                 method=kw.pop("method", ApprovalMethod.PER_BUNDLE),
                                 seconds_on_screen=kw.pop("seconds_on_screen", 42.0), clock=self.clock,
                                 audit=self.audit, resolver=self.links.get, **kw)
        a = granted.approval
        self.approvals[a.id] = a
        self.bundle = self.bundle.model_copy(update={"state": BundleState.APPROVED,
                                                     "root_hash": granted.bundle_hash.root})
        for vid, v in list(self.variants.items()):
            self.variants[vid] = v.model_copy(update={"state": VariantState.SCHEDULED,
                                                      "content_sha256": granted.bundle_hash.variants[vid]})
        return a

    def edit_variant(self, vid: str, **update) -> None:
        self.variants[vid] = self.variants[vid].model_copy(update=update)

    def edit_claim(self, cid: str, **update) -> None:
        self.claims[cid] = self.claims[cid].model_copy(update=update)


def make_world(clock: FakeClock | None = None) -> World:
    clock = clock or FakeClock(FAKE_EPOCH)
    now = clock.now()
    tenant = Tenant(id=ident(clock), created_at=now, name="Demo Bikes", slug=f"demo-{next(_counter)}",
                    default_language=Language.IT, region_code="IT")
    t = tenant.id
    user_id = ident(clock)
    bundle = Bundle(id=ident(clock), created_at=now, tenant_id=t, asset_id=ident(clock),
                    state=BundleState.IN_REVIEW, rule_version=3)
    blobs = {"s3://bucket/hero.png": b"\x89PNG hero image bytes", "s3://bucket/detail.png": b"\x89PNG detail"}
    media = {}
    for uri, data in blobs.items():
        m = Media(id=ident(clock), created_at=now, tenant_id=t, kind=MediaKind.IMAGE,
                  sha256=hashlib.sha256(data).hexdigest(), mime="image/png", size_bytes=len(data),
                  storage_uri=uri, provenance=MediaProvenance.CLIENT_PHOTO, alt_text="A red bike",
                  connection_id=None, consent_id=None)
        media[m.id] = m
    claim = Claim(id=ident(clock), created_at=now, tenant_id=t, code="C-0012", kind=ClaimKind.NUMBER,
                  state=ClaimState.VERIFIED, value="120", unit="km", evidence_sha256="a" * 64, source_id=None,
                  verified_by=user_id, verified_at=now, expires_at=now + timedelta(days=60),
                  languages={"it": "120 km di autonomia"})
    v1 = Variant(id=ident(clock), created_at=now, tenant_id=t, bundle_id=bundle.id, channel=Channel.INSTAGRAM,
                 language=Language.IT, state=VariantState.REVIEW, text="Fino a {{C-0012}} con una carica.",
                 rendered_text="Fino a 120 km con una carica. https://thallo.test/r/abc", task_type="offer_carousel")
    v2 = Variant(id=ident(clock), created_at=now, tenant_id=t, bundle_id=bundle.id,
                 channel=Channel.LINKEDIN_COMPANY, language=Language.EN, state=VariantState.REVIEW,
                 text="Our new bike is here.", rendered_text="Our new bike is here.", task_type="linkedin_text")
    mids = list(media)
    vm = {v1.id: [VariantMedia(id=ident(clock), created_at=now, tenant_id=t, variant_id=v1.id, media_id=mids[0],
                               position=0),
                  VariantMedia(id=ident(clock), created_at=now, tenant_id=t, variant_id=v1.id, media_id=mids[1],
                               position=1)]}
    return World(clock=clock, tenant=tenant, bundle=bundle, variants={v1.id: v1, v2.id: v2}, media=media,
                 variant_media=vm, claims={claim.id: claim}, variant_claims={v1.id: [claim.id]}, blobs=blobs,
                 links={"https://thallo.test/r/abc": "https://demo-bikes.example/offer?utm=ig"}, user_id=user_id)


@pytest.fixture
def world() -> World:
    return make_world()
