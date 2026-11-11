"""The gate. Every outbound action passes here. Nothing else may call a publisher.

`legacy` keeps the original engine's Gate and content_hash working until WP-08 ports the
publisher. The bundle gate (WP-04): `canonical` (root hash, see CANONICAL_FORM.md),
`approvals` (human-only, hash-bound, expiring) and `decision` (the publish-time check).
"""
from cortex_operator.gate.approvals import (
    DEFAULT_APPROVAL_TTL,
    MAX_APPROVAL_TTL,
    ApprovalRefused,
    Granted,
    approve_bundle,
    is_expired,
    void_approval,
)
from cortex_operator.gate.canonical import (
    BundleContent,
    BundleHash,
    MediaItem,
    VariantContent,
    bundle_hash,
    canonical_text,
)
from cortex_operator.gate.decision import (
    Block,
    BlockReason,
    GateDecision,
    GateRepository,
    SpendCheck,
    check_publish,
    decide,
    load_bundle,
)
from cortex_operator.gate.legacy import Gate, content_hash

__all__ = [
    "DEFAULT_APPROVAL_TTL",
    "MAX_APPROVAL_TTL",
    "ApprovalRefused",
    "Block",
    "BlockReason",
    "BundleContent",
    "BundleHash",
    "Gate",
    "GateDecision",
    "GateRepository",
    "Granted",
    "MediaItem",
    "SpendCheck",
    "VariantContent",
    "approve_bundle",
    "bundle_hash",
    "canonical_text",
    "check_publish",
    "content_hash",
    "decide",
    "is_expired",
    "load_bundle",
    "void_approval",
]
