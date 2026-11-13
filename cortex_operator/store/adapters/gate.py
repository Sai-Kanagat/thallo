"""The gate's `GateRepository` over Postgres (WP-01 follow-up to WP-04).

One instance wraps one tenant transaction (`Database.tenant`), so everything `check_publish` or
`approve_bundle_from_repo` reads comes from the same connection, behind row-level security. A
call naming another tenant raises, and the gate turns any raise into a block.

    with database.tenant(tenant_id) as repo:
        gate = PgGateRepository(repo, blobs, redirect_prefixes=("https://go.example/r/",))
        decision = check_publish(gate, tenant_id=tenant_id, ...)

Links: a URL under one of `redirect_prefixes` resolves through this tenant's `tracked_link` row
for that slug. A direct URL resolves to itself only if it is in `vouched_links`. Anything else is
unknown (None), so the gate blocks it. No network. The tenant allowlist of non-link tokens and the
vouched direct links have no table yet; they are passed in until the link package (WP-12) adds one.
"""
from __future__ import annotations

from collections.abc import Collection, Sequence

from cortex_operator.contracts.enums import ApprovalSubject
from cortex_operator.contracts.models import (
    Approval,
    AppUser,
    AuditEntry,
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
from cortex_operator.gate.canonical import normalise_url
from cortex_operator.store.adapters.audit import PgAuditStore, own
from cortex_operator.store.adapters.blobs import BlobStore
from cortex_operator.store.db import TenantRepo

_SLUG_END = frozenset("/?#")


def read_system_paused(repo: TenantRepo) -> bool:
    """The global kill switch (migration 0013). A missing row, or anything but an explicit false,
    means paused."""
    row = repo.execute("SELECT system_paused FROM system_setting WHERE id").fetchone()
    return row is None or row[0] is not False


class PgGateRepository:
    def __init__(self, repo: TenantRepo, blobs: BlobStore, *, redirect_prefixes: Sequence[str] = (),
                 vouched_links: Collection[str] = (), non_links: Collection[str] = (),
                 anchor: tuple[int, str] | None = None) -> None:
        self.repo = repo
        self.blobs = blobs
        self.redirect_prefixes = tuple(normalise_url(p) for p in redirect_prefixes)
        self.vouched_links = frozenset(normalise_url(u) for u in vouched_links)
        self.non_links = frozenset(non_links)
        self.anchor = anchor
        self.audit = PgAuditStore(repo)

    def system_paused(self) -> bool:
        return read_system_paused(self.repo)

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        own(self.repo, tenant_id)
        return self.repo.get(Tenant, tenant_id)

    def get_bundle(self, tenant_id: str, bundle_id: str) -> Bundle | None:
        own(self.repo, tenant_id)
        return self.repo.get(Bundle, bundle_id)

    def get_variants(self, tenant_id: str, bundle_id: str) -> Sequence[Variant]:
        own(self.repo, tenant_id)
        return self.repo.list(Variant, bundle_id=bundle_id)

    def get_variant_media(self, tenant_id: str, variant_id: str) -> Sequence[tuple[VariantMedia, Media]]:
        own(self.repo, tenant_id)
        out = []
        for link in self.repo.list(VariantMedia, variant_id=variant_id):
            media = self.repo.get(Media, link.media_id)
            if media is None:
                raise LookupError(f"media {link.media_id} is missing")
            out.append((link, media))
        return out

    def read_media_bytes(self, tenant_id: str, media: Media) -> bytes:
        own(self.repo, tenant_id)
        if media.tenant_id != tenant_id:
            raise PermissionError("media belongs to another tenant")
        return self.blobs.read(tenant_id, media.storage_uri)

    def get_variant_claims(self, tenant_id: str, variant_id: str) -> Sequence[Claim]:
        own(self.repo, tenant_id)
        return self.repo.query(Claim, "SELECT c.* FROM claim c JOIN variant_claim vc "
                                      "ON vc.tenant_id = c.tenant_id AND vc.claim_id = c.id "
                                      "WHERE vc.variant_id = %s ORDER BY c.code, c.id", (variant_id,))

    def get_tracked_links(self, tenant_id: str, variant_id: str) -> Sequence[TrackedLink]:
        own(self.repo, tenant_id)
        return self.repo.list(TrackedLink, variant_id=variant_id)

    def get_consent(self, tenant_id: str, consent_id: str) -> Consent | None:
        own(self.repo, tenant_id)
        return self.repo.get(Consent, consent_id)

    def get_approval(self, tenant_id: str, approval_id: str) -> Approval | None:
        own(self.repo, tenant_id)
        return self.repo.get(Approval, approval_id)

    def get_approvals(self, tenant_id: str, bundle_id: str) -> Sequence[Approval]:
        own(self.repo, tenant_id)
        return self.repo.list(Approval, subject_kind=ApprovalSubject.BUNDLE, subject_id=bundle_id)

    def get_user(self, tenant_id: str, user_id: str) -> AppUser | None:
        own(self.repo, tenant_id)
        return self.repo.get(AppUser, user_id)

    def get_audit_entries(self, tenant_id: str, subject_kind: str, subject_id: str) -> Sequence[AuditEntry]:
        return self.audit.about(tenant_id, subject_kind, subject_id)

    def get_check_results(self, tenant_id: str, variant_id: str) -> Sequence[CheckResult]:
        own(self.repo, tenant_id)
        return self.repo.query(CheckResult, "SELECT * FROM check_result WHERE variant_id = %s ORDER BY id",
                               (variant_id,))

    def get_audit_chain(self, tenant_id: str, from_seq: int) -> Sequence[AuditEntry]:
        return self.audit.since(tenant_id, from_seq)

    def audit_anchor(self, tenant_id: str) -> tuple[int, str] | None:
        own(self.repo, tenant_id)
        return self.anchor

    def non_link_allowlist(self, tenant_id: str) -> Collection[str]:
        own(self.repo, tenant_id)
        return self.non_links

    def resolve_link(self, tenant_id: str, url: str) -> str | None:
        own(self.repo, tenant_id)
        url = normalise_url(url)
        for prefix in self.redirect_prefixes:
            slug = url[len(prefix):] if url.startswith(prefix) else ""
            if slug and not (_SLUG_END & set(slug)):
                rows = self.repo.list(TrackedLink, slug=slug)
                return rows[0].destination if rows else None
        return url if url in self.vouched_links else None


__all__ = ["PgGateRepository", "read_system_paused"]
