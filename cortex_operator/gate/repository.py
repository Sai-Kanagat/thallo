"""What the gate reads, and the one loader that both approval and publish use (WP-04).

WP-01 implements `GateRepository` over Postgres. Every call is tenant-scoped, but the gate
re-checks tenant ids itself and never trusts the repository to have filtered.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from typing import Protocol

from cortex_operator.contracts.models import (
    Approval,
    AppUser,
    AuditEntry,
    Bundle,
    Claim,
    Consent,
    Media,
    Tenant,
    TrackedLink,
    Variant,
    VariantMedia,
)
from cortex_operator.gate.canonical import BundleContent, MediaItem, VariantContent


class GateRepository(Protocol):
    def system_paused(self) -> bool:
        """The global kill switch. True stops every publish for every tenant."""
        ...

    def get_tenant(self, tenant_id: str) -> Tenant | None: ...

    def get_bundle(self, tenant_id: str, bundle_id: str) -> Bundle | None: ...

    def get_variants(self, tenant_id: str, bundle_id: str) -> Sequence[Variant]: ...

    def get_variant_media(self, tenant_id: str, variant_id: str) -> Sequence[tuple[VariantMedia, Media]]: ...

    def read_media_bytes(self, tenant_id: str, media: Media) -> bytes | Iterable[bytes]:
        """The stored file's current bytes, read fresh from storage (not a cached hash)."""
        ...

    def get_variant_claims(self, tenant_id: str, variant_id: str) -> Sequence[Claim]: ...

    def get_tracked_links(self, tenant_id: str, variant_id: str) -> Sequence[TrackedLink]:
        """Every tracked link whose variant_id is this variant."""
        ...

    def get_consent(self, tenant_id: str, consent_id: str) -> Consent | None: ...

    def get_approval(self, tenant_id: str, approval_id: str) -> Approval | None: ...

    def get_approvals(self, tenant_id: str, bundle_id: str) -> Sequence[Approval]:
        """Every approval whose subject is this bundle, voided or not."""
        ...

    def get_user(self, tenant_id: str, user_id: str) -> AppUser | None: ...

    def get_audit_entries(self, tenant_id: str, subject_kind: str, subject_id: str) -> Sequence[AuditEntry]:
        """Audit entries about one subject, for example ("approval", approval_id)."""
        ...

    def get_audit_chain(self, tenant_id: str, from_seq: int) -> Sequence[AuditEntry]:
        """Every audit entry with seq >= from_seq, in seq order, through the current head."""
        ...

    def audit_anchor(self, tenant_id: str) -> tuple[int, str] | None:
        """The last (seq, entry_hash) recorded outside the database by WP-20, or None."""
        ...

    def non_link_allowlist(self, tenant_id: str) -> Collection[str]:
        """Tokens the tenant vouched are not links (for example a product name with a dot)."""
        ...

    def resolve_link(self, tenant_id: str, url: str) -> str | None:
        """Final destination of a normalised link: the tracked link table, known redirects, or the
        URL itself for a direct link the tenant has vouched for. None when unknown. No network."""
        ...


class MediaUnreadable(RuntimeError):
    pass


def load_bundle(repo: GateRepository, tenant_id: str, bundle: Bundle) -> BundleContent:
    """Snapshot a bundle, hashing each media file from its bytes as read now. Raises on any
    repository failure; `check_publish` turns that into a block."""
    variants: list[VariantContent] = []
    for v in repo.get_variants(tenant_id, bundle.id):
        items: list[MediaItem] = []
        for link, media in repo.get_variant_media(tenant_id, v.id):
            try:
                data = repo.read_media_bytes(tenant_id, media)
                item = MediaItem.from_bytes(media, link.position, data)
            except Exception as exc:  # any storage failure blocks; it never passes
                raise MediaUnreadable(f"media {media.id}: {type(exc).__name__}") from exc
            consent = repo.get_consent(tenant_id, media.consent_id) if media.consent_id else None
            items.append(MediaItem(media=media, position=item.position, bytes_sha256=item.bytes_sha256,
                                   consent=consent))
        variants.append(VariantContent(
            variant=v, media=tuple(items), claims=tuple(repo.get_variant_claims(tenant_id, v.id)),
            tracked_links=tuple(repo.get_tracked_links(tenant_id, v.id))))
    return BundleContent(bundle=bundle, variants=tuple(variants))


__all__ = ["GateRepository", "MediaUnreadable", "load_bundle"]
