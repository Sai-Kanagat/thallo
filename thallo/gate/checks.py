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
compiler: `truth.compile` or `truth.compile.<kind>` for a kind the compiler has. Media is covered
per attached item (lead ruling, WP-07), keyed on the item's bytes hash (`check_result.media_sha256`)
and chosen by its source (`MEDIA_COVERAGE`): a template render needs its design-lint rows (a name
in `thallo.media.lint.CHECKS`); a client photo needs a photo check row (code, no model) AND the
photo attestation FLAG the approver acknowledges; generated imagery needs its provenance row (FAIL
while generation is off, FLAG on every use when an operator turns it on). Any other or unknown
source never satisfies coverage. A name outside the tables never satisfies coverage, though its
FAIL and FLAG rows still count. A variant missing any required type or item has not been checked
and its grant is refused.

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
# MEDIA is never satisfied at variant level: each attached item is covered on its own (`MEDIA_COVERAGE`).
COVERAGE: Mapping[ContentType, frozenset[str]] = {
    ContentType.TEXT: frozenset({TRUTH_CHECK, *(f"{TRUTH_CHECK}.{k.value}" for k in FindingKind)}),
    ContentType.MEDIA: frozenset(DESIGN_LINT_CHECKS),
}
ALL_TYPES = frozenset(ContentType)

# Media checks written by the check stack (thallo/checks). Kept here so the gate owns the names.
PHOTO_CHECK = "media.photo"  # resolution, blur, exposure, crop: code, no model
PHOTO_CHECKS = (PHOTO_CHECK, f"{PHOTO_CHECK}.resolution", f"{PHOTO_CHECK}.blur", f"{PHOTO_CHECK}.exposure",
                f"{PHOTO_CHECK}.crop")
PHOTO_ATTESTATION = "media.photo.attestation"  # a FLAG the approver acknowledges: "this is the client's photo"
PROVENANCE_CHECK = "media.provenance"  # generated imagery: FAIL while off, FLAG on every use when on

GENERATED = frozenset({"generated", "generated_background"})
# Names that only cover an item when their verdict is FLAG (a FAIL still blocks; a PASS never covers).
MUST_FLAG = frozenset({PHOTO_ATTESTATION, PROVENANCE_CHECK})

# Per media source (media.provenance), the families an item needs: every family must have at least
# one counting CODE-layer row whose `media_sha256` is the item's bytes hash. A source missing here
# (client clip, client graphic, a person's file or recording, or no source known) is never covered.
MEDIA_COVERAGE: Mapping[str, tuple[frozenset[str], ...]] = {
    "template_render": (frozenset(DESIGN_LINT_CHECKS),),
    "client_photo": (frozenset(PHOTO_CHECKS), frozenset({PHOTO_ATTESTATION})),
    "generated": (frozenset({PROVENANCE_CHECK}),),
    "generated_background": (frozenset({PROVENANCE_CHECK}),),
}


@dataclass(frozen=True)
class MediaNeed:
    """One attached media item the gate must see checked: its bytes hash and its source."""

    sha256: str
    provenance: str | None

    @property
    def value(self) -> str:  # shown the way a ContentType is: "media.<source>.<hash prefix>"
        return f"media.{self.provenance or 'unknown_source'}.{self.sha256[:12]}"

    def families(self) -> tuple[frozenset[str], ...]:
        return MEDIA_COVERAGE.get(self.provenance or "", ())


Need = ContentType | MediaNeed


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


def content_types(payload: PublishPayload) -> frozenset[Need]:
    """What a variant carries, from the payload that is hashed and sent: TEXT always, and for media
    MEDIA plus one `MediaNeed` per attached item (bytes hash and source)."""
    media: tuple[Need, ...] = ()
    if payload.media:
        media = (ContentType.MEDIA, *(MediaNeed(m.sha256, getattr(m, "provenance", None)) for m in payload.media))
    return frozenset({ContentType.TEXT, *media})


def _media_covered(need: MediaNeed, rows: Sequence[CheckResult]) -> bool:
    families = need.families()
    if not families:
        return False  # an unknown source never satisfies coverage
    # A row that stands for a person's look must be a FLAG the approver acknowledges, never a PASS:
    # the photo attestation, and the provenance row of generated imagery.
    names = {r.check_name for r in rows if r.layer is CheckLayer.CODE and r.media_sha256 == need.sha256
             and (r.check_name not in MUST_FLAG or r.verdict is CheckVerdict.FLAG)}
    return all(names & family for family in families)


def covers(check_name: str, kind: ContentType) -> bool:
    return check_name in COVERAGE[kind]


@dataclass(frozen=True)
class CheckReview:
    failing: frozenset[str]  # FAIL row ids
    flags: frozenset[str]  # FLAG row ids that need acknowledging
    unchecked: tuple[str, ...]  # variant ids missing a counting row for some required content type
    counted: tuple[CheckResult, ...]  # the rows that decided, for display
    missing: Mapping[str, tuple[Need, ...]]  # per unchecked variant, the types or media items not covered


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
                  types: Mapping[str, Collection[Need]]) -> CheckReview:
    """`hashes` maps each variant id under review to its current leaf hash, `types` to what it
    carries (`content_types`). A variant with no entry in `types` must cover every type.

    TEXT is covered by a counting CODE row named in `COVERAGE[TEXT]`. MEDIA is covered item by item:
    each `MediaNeed` needs every family of its source (`MEDIA_COVERAGE`) on rows carrying its bytes
    hash. MEDIA with no `MediaNeed` beside it (the caller did not say which items, or from where)
    is not covered: fail closed."""
    counted = counting_rows(results, tenant_id=tenant_id, hashes=hashes)
    decisive = decisive_rows(counted)
    missing: dict[str, tuple[Need, ...]] = {}
    for vid in sorted(hashes):
        mine = [r for r in counted if r.variant_id == vid]
        names = {r.check_name for r in mine if r.layer is CheckLayer.CODE}
        declared = frozenset(types.get(vid, ALL_TYPES)) | {ContentType.TEXT}
        items = sorted((n for n in declared if isinstance(n, MediaNeed)), key=lambda n: (n.sha256, n.provenance or ""))
        gap: list[Need] = []
        if not names & COVERAGE[ContentType.TEXT]:
            gap.append(ContentType.TEXT)
        if ContentType.MEDIA in declared and not items:
            gap.append(ContentType.MEDIA)
        gap += [n for n in items if not _media_covered(n, mine)]
        if gap:
            missing[vid] = tuple(gap)
    return CheckReview(
        failing=frozenset(r.id for r in decisive if r.verdict is CheckVerdict.FAIL),
        flags=frozenset(r.id for r in decisive if r.verdict is CheckVerdict.FLAG),
        unchecked=tuple(missing),
        counted=tuple(decisive),
        missing=missing,
    )


__all__ = [
    "ALL_TYPES", "COVERAGE", "GENERATED", "MEDIA_COVERAGE", "MUST_FLAG", "PHOTO_ATTESTATION", "PHOTO_CHECK",
    "PHOTO_CHECKS",
    "PROVENANCE_CHECK", "CheckHashError", "CheckReader", "CheckReview", "ContentType", "MediaNeed", "Need",
    "check_hash", "content_types", "counting_rows", "covers", "decisive_rows", "review_checks",
]
