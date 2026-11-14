"""The gate. Every outbound action passes here. Nothing else may call a publisher.

`legacy` keeps the original engine's Gate and content_hash working until WP-08 ports the
publisher. The bundle gate (WP-04): `canonical` (root hash and payload, see CANONICAL_FORM.md),
`repository` (what the gate reads, one loader for approval and publish), `approvals`
(human-only, hash-bound, expiring, returned as one transaction, see AUDIT.md) and `decision`
(the publish-time check).
"""
from thallo.gate.approvals import (
    APPROVER_ROLES,
    DEFAULT_APPROVAL_TTL,
    MAX_APPROVAL_TTL,
    ApprovalRefused,
    GateChange,
    approve_bundle,
    approve_bundle_from_repo,
    is_expired,
    void_bundle_approvals,
)
from thallo.gate.canonical import (
    BundleContent,
    BundleHash,
    MediaItem,
    PayloadMedia,
    PublishPayload,
    VariantContent,
    bundle_hash,
    canonical_text,
    find_unresolved_links,
    variant_hash,
)
from thallo.gate.checks import CheckReader, CheckReview, review_checks
from thallo.gate.decision import (
    ApprovalEvidence,
    Block,
    BlockReason,
    GateDecision,
    SpendCheck,
    check_publish,
    decide,
)
from thallo.gate.legacy import Gate, content_hash
from thallo.gate.repository import GateRepository, MediaUnreadable, content_hashes, load_bundle
from thallo.gate.signing import ENV_KEY, GateKeyMissing, GateSigner

__all__ = [
    "CheckReader",
    "CheckReview",
    "content_hashes",
    "review_checks",
    "variant_hash",
    "ENV_KEY",
    "GateKeyMissing",
    "GateSigner",
    "find_unresolved_links",
    "APPROVER_ROLES",
    "DEFAULT_APPROVAL_TTL",
    "MAX_APPROVAL_TTL",
    "ApprovalEvidence",
    "ApprovalRefused",
    "Block",
    "BlockReason",
    "BundleContent",
    "BundleHash",
    "Gate",
    "GateChange",
    "GateDecision",
    "GateRepository",
    "MediaItem",
    "MediaUnreadable",
    "PayloadMedia",
    "PublishPayload",
    "SpendCheck",
    "VariantContent",
    "approve_bundle",
    "approve_bundle_from_repo",
    "bundle_hash",
    "canonical_text",
    "check_publish",
    "content_hash",
    "decide",
    "is_expired",
    "load_bundle",
    "void_bundle_approvals",
]
