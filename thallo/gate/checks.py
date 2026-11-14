"""Which check results count, and what they say (lead ruling, contracts 2.5.0).

A `check_result` row is bound to the content it checked by `content_sha256`, the variant's leaf
hash in the bundle root. Only rows whose hash is the variant's current hash count; rows about any
other content are ignored, for and against. So fixing the content is what clears a FAIL, and
restoring the old content brings the old FAIL back.

Among the counting rows, each (variant, layer, check name) is decided by the rows at its highest
attempt. One check may emit several rows at one attempt (one per finding) and every one of them
counts. Then:
  FAIL  refuses the grant and blocks publish. It cannot be acknowledged.
  FLAG  must be acknowledged by the approver, one id per row.
  PASS  collapses.
A variant with no counting CODE-layer row has not been checked and its grant is refused.

Approval (`approvals.approve_bundle`) and publish (`decision.decide`) both call `review_checks`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from thallo.contracts.enums import CheckLayer, CheckVerdict
from thallo.contracts.models import CheckResult

# Reads the rows of one variant for one content hash: (tenant_id, variant_id, content_sha256).
CheckReader = Callable[[str, str, str], Sequence[CheckResult]]


@dataclass(frozen=True)
class CheckReview:
    failing: frozenset[str]  # FAIL row ids
    flags: frozenset[str]  # FLAG row ids that need acknowledging
    unchecked: tuple[str, ...]  # variant ids with no counting CODE-layer row
    counted: tuple[CheckResult, ...]  # the rows that decided, for display


def counting_rows(results: Sequence[CheckResult], *, tenant_id: str,
                  hashes: Mapping[str, str]) -> list[CheckResult]:
    """Rows of this tenant about a variant in `hashes` whose content hash is that variant's
    current one. The repository is never trusted to have filtered."""
    return [r for r in results
            if r.tenant_id == tenant_id and r.variant_id in hashes and r.content_sha256 == hashes[r.variant_id]]


def decisive_rows(rows: Sequence[CheckResult]) -> list[CheckResult]:
    """Every row at the highest attempt of its (variant, layer, check name)."""
    by_key: dict[tuple[str, str, str], list[CheckResult]] = {}
    for r in rows:
        by_key.setdefault((r.variant_id, r.layer.value, r.check_name), []).append(r)
    out: list[CheckResult] = []
    for group in by_key.values():
        top = max(r.attempt for r in group)
        out += [r for r in group if r.attempt == top]
    return sorted(out, key=lambda r: r.id)


def review_checks(results: Sequence[CheckResult], *, tenant_id: str, hashes: Mapping[str, str]) -> CheckReview:
    """`hashes` maps each variant id under review to its current leaf hash."""
    counted = counting_rows(results, tenant_id=tenant_id, hashes=hashes)
    decisive = decisive_rows(counted)
    covered = {r.variant_id for r in counted if r.layer is CheckLayer.CODE}
    return CheckReview(
        failing=frozenset(r.id for r in decisive if r.verdict is CheckVerdict.FAIL),
        flags=frozenset(r.id for r in decisive if r.verdict is CheckVerdict.FLAG),
        unchecked=tuple(sorted(vid for vid in hashes if vid not in covered)),
        counted=tuple(decisive),
    )


__all__ = ["CheckReader", "CheckReview", "counting_rows", "decisive_rows", "review_checks"]
