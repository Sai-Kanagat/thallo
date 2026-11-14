"""A small in-memory world for the gate tests: one tenant, one bundle in review, two variants,
media in a fake blob store addressed by path, verified claims, users, consents and tracked links.
`World` implements GateRepository; `fail_on` makes one repository method raise."""
from __future__ import annotations

import hashlib
import itertools
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import (
    ApprovalMethod,
    Channel,
    ClaimKind,
    ConsentBasis,
    ConsentScope,
    Language,
    MediaKind,
    MediaProvenance,
    UserRole,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Approval,
    AppUser,
    Bundle,
    CheckResult,
    Claim,
    Consent,
    Media,
    Tenant,
    TrackedLink,
    Variant,
    VariantMedia,
)
from thallo.contracts.states import BundleState, ClaimState, VariantState
from thallo.gate import (
    GateChange,
    approve_bundle_from_repo,
    bundle_hash,
    load_bundle,
    void_bundle_approvals,
)
from thallo.gate.signing import GateSigner
from thallo.trust.audit import AuditLog, InMemoryAuditStore

_counter = itertools.count(1)


def ident(clock: FakeClock) -> str:
    return new_id(clock, next(_counter).to_bytes(10, "big"))


class FakeVerifier:
    def __init__(self, live: set[str]) -> None:
        self.live = live

    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id in self.live


class RepoDown(RuntimeError):
    pass


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
    users: dict[str, AppUser]
    tracked: dict[str, list[TrackedLink]]
    approvals: dict[str, Approval] = field(default_factory=dict)
    consents: dict[str, Consent] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    audit_store: InMemoryAuditStore = field(default_factory=InMemoryAuditStore)
    user_id: str = ""
    session: str = "live-1"
    paused_everywhere: bool = False
    fail_on: str | None = None
    non_links: set[str] = field(default_factory=set)
    checks: list[CheckResult] = field(default_factory=list)
    anchor: tuple[int, str] | None = None
    signer: GateSigner = field(default_factory=lambda: GateSigner(secrets.token_bytes(32)))

    def _ok(self, name: str) -> None:
        if self.fail_on == name:
            raise RepoDown(name)

    # ----- GateRepository
    def system_paused(self):
        self._ok("system_paused")
        return self.paused_everywhere

    def get_tenant(self, tenant_id):
        self._ok("get_tenant")
        return self.tenant if self.tenant.id == tenant_id else None

    def get_bundle(self, tenant_id, bundle_id):
        self._ok("get_bundle")
        b = self.bundle
        return b if b.id == bundle_id and b.tenant_id == tenant_id else None

    def get_variants(self, tenant_id, bundle_id):
        self._ok("get_variants")
        return [v for v in self.variants.values() if v.bundle_id == bundle_id and v.tenant_id == tenant_id]

    def get_variant_media(self, tenant_id, variant_id):
        self._ok("get_variant_media")
        return [(vm, self.media[vm.media_id]) for vm in self.variant_media.get(variant_id, [])
                if vm.tenant_id == tenant_id]

    def read_media_bytes(self, tenant_id, media):
        self._ok("read_media_bytes")
        return self.blobs[media.storage_uri]

    def get_variant_claims(self, tenant_id, variant_id):
        self._ok("get_variant_claims")
        return [self.claims[c] for c in self.variant_claims.get(variant_id, [])]

    def get_tracked_links(self, tenant_id, variant_id):
        self._ok("get_tracked_links")
        return list(self.tracked.get(variant_id, []))

    def get_consent(self, tenant_id, consent_id):
        self._ok("get_consent")
        c = self.consents.get(consent_id)
        return c if c is not None and c.tenant_id == tenant_id else None

    def get_approval(self, tenant_id, approval_id):
        self._ok("get_approval")
        a = self.approvals.get(approval_id)
        return a if a is not None and a.tenant_id == tenant_id else None

    def get_approvals(self, tenant_id, bundle_id):
        self._ok("get_approvals")
        return [a for a in self.approvals.values() if a.tenant_id == tenant_id and a.subject_id == bundle_id]

    def get_user(self, tenant_id, user_id):
        self._ok("get_user")
        return self.users.get(user_id)  # deliberately not tenant-filtered: the gate must check

    def get_audit_entries(self, tenant_id, subject_kind, subject_id):
        self._ok("get_audit_entries")
        return [e for e in self.audit_store.entries(tenant_id)
                if e.subject_kind == subject_kind and e.subject_id == subject_id]

    def get_audit_chain(self, tenant_id, from_seq):
        self._ok("get_audit_chain")
        return [e for e in self.audit_store.entries(tenant_id) if e.seq >= from_seq]

    def audit_anchor(self, tenant_id):
        self._ok("audit_anchor")
        return self.anchor

    def non_link_allowlist(self, tenant_id):
        self._ok("non_link_allowlist")
        return frozenset(self.non_links)

    def get_check_results(self, tenant_id, variant_id):
        self._ok("get_check_results")
        return [c for c in self.checks if c.variant_id == variant_id and c.tenant_id == tenant_id]

    def resolve_link(self, tenant_id, url):
        self._ok("resolve_link")
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

    def apply(self, change: GateChange) -> None:
        """What WP-01 does in one transaction."""
        for e in change.audit_entries:
            self.audit_store.insert(e)
        for a in change.voided_approvals:
            self.approvals[a.id] = a
        if change.new_approval is not None:
            self.approvals[change.new_approval.id] = change.new_approval
        self.bundle = change.bundle
        for v in change.variants:
            self.variants[v.id] = v

    def approve(self, *, return_change: bool = False, **kw):
        """Approve as the verified human through the repository path, then commit the change."""
        shown = bundle_hash(self.content(), self.links.get, frozenset(self.non_links)).root
        change = approve_bundle_from_repo(
            self, tenant_id=self.tenant.id, bundle_id=self.bundle.id, actor=self.human(), verifier=self.verifier(),
            shown_hash=shown, method=kw.pop("method", ApprovalMethod.PER_BUNDLE),
            seconds_on_screen=kw.pop("seconds_on_screen", 42.0), clock=self.clock, audit=self.audit,
            signer=self.signer, **kw)
        self.apply(change)
        return change if return_change else change.new_approval

    def edit_variant(self, vid: str, **update) -> None:
        """A raw storage write, with no gate involvement."""
        self.variants[vid] = self.variants[vid].model_copy(update=update)

    def edit_after_grant(self, vid: str, **update) -> None:
        """The documented edit path: the edit and the voiding commit together."""
        change = void_bundle_approvals(self.bundle, tuple(self.variants.values()),
                                       tuple(self.approvals.values()), reason="text edited after approval",
                                       actor=self.human(), clock=self.clock, audit=self.audit,
                                       signer=self.signer)
        self.apply(change)
        self.edit_variant(vid, **update)

    def back_to_review(self) -> None:
        self.bundle = self.bundle.model_copy(update={"state": BundleState.IN_REVIEW})
        for vid in self.variants:
            self.edit_variant(vid, state=VariantState.REVIEW)

    def edit_claim(self, cid: str, **update) -> None:
        self.claims[cid] = self.claims[cid].model_copy(update=update)

    def add_consent(self) -> str:
        c = Consent(id=ident(self.clock), created_at=self.clock.now(), tenant_id=self.tenant.id, person_id=None,
                    subject_label="Rider in hero photo", basis=ConsentBasis.OPT_IN, scope=ConsentScope.LIKENESS,
                    evidence="signed release, scan 14", given_at=self.clock.now())
        self.consents[c.id] = c
        return c.id


def make_world(clock: FakeClock | None = None) -> World:
    clock = clock or FakeClock(FAKE_EPOCH)
    now = clock.now()
    tenant = Tenant(id=ident(clock), created_at=now, name="Demo Bikes", slug=f"demo-{next(_counter)}",
                    default_language=Language.IT, region_code="IT", operator_id=ident(clock), currency="EUR")
    t = tenant.id
    user = AppUser(id=ident(clock), created_at=now, tenant_id=t, display_name="Approver", role=UserRole.APPROVER,
                   identity_id=ident(clock))
    user_id = user.id
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
    tracked = {v1.id: [TrackedLink(id=ident(clock), created_at=now, tenant_id=t, variant_id=v1.id, ad_id=None,
                                   slug="abc", destination="https://demo-bikes.example/offer?utm=ig")]}
    return World(clock=clock, tenant=tenant, bundle=bundle, variants={v1.id: v1, v2.id: v2}, media=media,
                 variant_media=vm, claims={claim.id: claim}, variant_claims={v1.id: [claim.id]}, blobs=blobs,
                 users={user_id: user}, tracked=tracked,
                 links={"https://thallo.test/r/abc": "https://demo-bikes.example/offer?utm=ig"}, user_id=user_id)


@pytest.fixture
def world() -> World:
    return make_world()
