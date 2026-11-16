"""Which check results count, and what they say (lead rulings, contracts 2.5.0).

A `check_result` row is bound to the content it checked by `content_sha256`, the variant's leaf
hash in the bundle root, computed by `check_hash` (the one gate function every writer uses). Only
rows whose hash is the variant's current hash count; rows about any other content are ignored, for
and against. So fixing the content is what clears a FAIL, and restoring the old content brings the
old FAIL back.

Among the counting rows, each (variant, layer, check name) is decided by the rows at its highest
attempt. One check may emit several rows at one attempt (one per finding) and every one of them
counts. Then:
  FAIL  refuses the grant and blocks publish. It cannot be acknowledged.
  FLAG  must be acknowledged by the approver, one id per row.
  PASS  collapses.
A rerun on unchanged content clears a FAIL because every check writes a row under each of its keys
on every run (the truth compiler writes PASS for each kind it did not raise), so the newer attempt
decides. A FAIL decides unless a PASS or FLAG at the key's top attempt, higher than the FAIL's, was
written after it (newer uuid7 id): a FAIL written late at a low attempt is never hidden.

Coverage is decided here, per content type, from `COVERAGE`. Every variant has text (possibly
empty, which the compiler still checks), so it needs a counting CODE-layer row from the truth
compiler: `truth.compile` or `truth.compile.<kind>` for a kind the compiler has. A variant with
media also needs a counting CODE-layer design-lint row (a name in `thallo.media.lint.CHECKS`). A
name outside the table never satisfies coverage, though its FAIL and FLAG rows still count. A
variant missing any required type has not been checked and its grant is refused.

Approval (`approvals.approve_bundle`) and publish (`decision.decide`) both call `review_checks`
with `content_types` of the same publish payload, so the rule is the same in both places.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from thallo.contracts.enums import CheckLayer, CheckVerdict
from thallo.contracts.models import CheckResult
from thallo.gate.canonical import LinkResolver, PublishPayload, VariantContent, variant_hash
from thallo.media.lint import CHECKS as DESIGN_LINT_CHECKS
from thallo.truth.compiler import CHECK_NAME as TRUTH_CHECK
from thallo.truth.findings import FindingKind

# Reads the rows of one variant for one content hash: (tenant_id, variant_id, content_sha256).
CheckReader = Callable[[str, str, str], Sequence[CheckResult]]


class ContentType(StrEnum):
    TEXT = "text"
    MEDIA = "media"


# The one coverage table: for each content type, the check names that count as having checked it.
COVERAGE: Mapping[ContentType, frozenset[str]] = {
    ContentType.TEXT: frozenset({TRUTH_CHECK, *(f"{TRUTH_CHECK}.{k.value}" for k in FindingKind)}),
    ContentType.MEDIA: frozenset(DESIGN_LINT_CHECKS),
}
ALL_TYPES = frozenset(ContentType)


class CheckHashError(ValueError):
    """A check writer could not supply the variant as approval will see it. No row is written."""


def check_hash(content: VariantContent, *, resolver: LinkResolver, non_links: Collection[str]) -> str:
    """The `content_sha256` for a check row: the variant's leaf hash exactly as approval computes
    it. `content` is the variant as approval will load it (its text and rendered text, its media
    with their bytes hashes, its linked claims, its tracked links); `resolver` and `non_links` are
    the repository's (`repo.resolve_link`, `repo.non_link_allowlist`). Nothing is guessed: a
    missing input raises `CheckHashError`, so no row is written that could never match."""
    if not isinstance(content, VariantContent):
        raise CheckHashError("check_hash needs the variant content as approval will load it")
    if resolver is None or not callable(resolver):
        raise CheckHashError("check_hash needs the repository's link resolver")
    if non_links is None or isinstance(non_links, str):
        raise CheckHashError("check_hash needs the repository's non-link allowlist (empty if it has none)")
    return variant_hash(content, resolver, frozenset(non_links))


def content_types(payload: PublishPayload) -> frozenset[ContentType]:
    """What a variant carries, from the payload that is hashed and sent."""
    return frozenset({ContentType.TEXT, *((ContentType.MEDIA,) if payload.media else ())})


def covers(check_name: str, kind: ContentType) -> bool:
    return check_name in COVERAGE[kind]


@dataclass(frozen=True)
class CheckReview:
    failing: frozenset[str]  # FAIL row ids
    flags: frozenset[str]  # FLAG row ids that need acknowledging
    unchecked: tuple[str, ...]  # variant ids missing a counting row for some required content type
    counted: tuple[CheckResult, ...]  # the rows that decided, for display
    missing: Mapping[str, tuple[ContentType, ...]]  # per unchecked variant, the types not covered


def counting_rows(results: Sequence[CheckResult], *, tenant_id: str,
                  hashes: Mapping[str, str]) -> list[CheckResult]:
    """Rows of this tenant about a variant in `hashes` whose content hash is that variant's
    current one. The repository is never trusted to have filtered."""
    return [r for r in results
            if r.tenant_id == tenant_id and r.variant_id in hashes and r.content_sha256 == hashes[r.variant_id]]


def _cleared(fail: CheckResult, group: Sequence[CheckResult], top: int) -> bool:
    """A FAIL is cleared only by a row of its key that is not a FAIL, sits at the key's top
    attempt (so it is itself decisive), is at a strictly higher attempt than the FAIL, and is
    newer (row ids are uuid7, ordered by creation time; see AUDIT.md for the clock this assumes).
    A PASS or FLAG at the FAIL's own attempt, or at any attempt below the top, never clears it."""
    return any(p.verdict is not CheckVerdict.FAIL and p.attempt == top and p.attempt > fail.attempt
               and p.id > fail.id for p in group)


def decisive_rows(rows: Sequence[CheckResult]) -> list[CheckResult]:
    """Per (variant, layer, check name): every non-FAIL row at the top attempt, plus every FAIL
    that no newer top-attempt non-FAIL row clears (see `_cleared`). Every row that clears a FAIL
    is in the output, so a FLAG that clears one must be acknowledged. A late FAIL is never hidden
    by an older, higher-numbered row, and the repair loop (FAIL at 1, then PASS or FLAG at 2)
    clears it."""
    by_key: dict[tuple[str, str, str], list[CheckResult]] = {}
    for r in rows:
        by_key.setdefault((r.variant_id, r.layer.value, r.check_name), []).append(r)
    out: list[CheckResult] = []
    for group in by_key.values():
        top = max(r.attempt for r in group)
        out += [r for r in group if (r.attempt == top and r.verdict is not CheckVerdict.FAIL)
                or (r.verdict is CheckVerdict.FAIL and not _cleared(r, group, top))]
    return sorted(out, key=lambda r: r.id)


def review_checks(results: Sequence[CheckResult], *, tenant_id: str, hashes: Mapping[str, str],
                  types: Mapping[str, Collection[ContentType]]) -> CheckReview:
    """`hashes` maps each variant id under review to its current leaf hash, `types` to what it
    carries (`content_types`). A variant with no entry in `types` must cover every type."""
    counted = counting_rows(results, tenant_id=tenant_id, hashes=hashes)
    decisive = decisive_rows(counted)
    missing: dict[str, tuple[ContentType, ...]] = {}
    for vid in sorted(hashes):
        names = {r.check_name for r in counted if r.variant_id == vid and r.layer is CheckLayer.CODE}
        need = sorted(frozenset(types.get(vid, ALL_TYPES)) | {ContentType.TEXT})
        gap = tuple(k for k in need if not names & COVERAGE[k])
        if gap:
            missing[vid] = gap
    return CheckReview(
        failing=frozenset(r.id for r in decisive if r.verdict is CheckVerdict.FAIL),
        flags=frozenset(r.id for r in decisive if r.verdict is CheckVerdict.FLAG),
        unchecked=tuple(missing),
        counted=tuple(decisive),
        missing=missing,
    )


__all__ = [
    "ALL_TYPES", "COVERAGE", "CheckHashError", "CheckReader", "CheckReview", "ContentType", "check_hash",
    "content_types", "counting_rows", "covers", "decisive_rows", "review_checks",
]
