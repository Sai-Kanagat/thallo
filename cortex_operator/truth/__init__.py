"""Truth library (WP-03): typed claims with proof and expiry, slot filling per language, and the
compile step that keeps numbers, superlatives, comparisons and names out of model-typed copy.

Modules
- `claims`: candidate, human-only verification, expiry, change with its void list
- `compiler`: `compile_text`, `scan`, `build` (compile, fill, check the output), the `Detector` hook
- `fill`: `render_claim` and per-language number, unit, price and date formatting. Filling text
  is private and reached only through `build`
- `units`: the units and ISO 4217 codes a claim may carry
- `segment`: sentence segmentation, for display only (not a security boundary)
- `lexicon`: per-language word lists, lookalike folding and script rules
- `repository`: the storage Protocols WP-01 implements, and an in-memory version
- `legacy`: the original engine's `ClaimLedger`, re-exported below so its imports keep working

`truth.Claim` is the legacy dataclass. The contract row is `cortex_operator.contracts.models.Claim`.
"""
from cortex_operator.truth.claims import (
    DEFAULT_POLICY,
    ClaimChange,
    ClaimError,
    ExpiryPolicy,
    change_claim,
    expire_due,
    new_candidate,
    next_code,
    reject_claim,
    verify_claim,
)
from cortex_operator.truth.compiler import (
    Built,
    CompileFailed,
    CompileResult,
    Detector,
    Finding,
    FindingKind,
    build,
    compile_text,
    scan,
)
from cortex_operator.truth.fill import Filled, FillError, LibraryError, render_claim
from cortex_operator.truth.legacy import REF, Claim, ClaimLedger, render
from cortex_operator.truth.repository import ClaimRepository, ClaimUsage, InMemoryClaimRepository
from cortex_operator.truth.segment import Sentence, segment

__all__ = [
    "DEFAULT_POLICY",
    "REF",
    "Built",
    "Claim",
    "ClaimChange",
    "ClaimError",
    "ClaimLedger",
    "ClaimRepository",
    "ClaimUsage",
    "CompileFailed",
    "CompileResult",
    "Detector",
    "ExpiryPolicy",
    "FillError",
    "Filled",
    "Finding",
    "FindingKind",
    "InMemoryClaimRepository",
    "LibraryError",
    "Sentence",
    "build",
    "change_claim",
    "compile_text",
    "expire_due",
    "new_candidate",
    "next_code",
    "reject_claim",
    "render",
    "render_claim",
    "scan",
    "segment",
    "verify_claim",
]
