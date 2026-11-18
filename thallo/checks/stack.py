"""The check stack (WP-07, BUILD_PLAN 2.7 and module 10, quality bar 2 to 4).

    outcome = run_stack(piece, ctx, repair=repairer)
    with db.tenant(tenant.id) as repo:
        write_stack(repo, outcome)

It runs on the FINISHED piece: the variant as `gate.load_bundle` returns it (text, rendered text,
media with their bytes hashes, claims, tracked links), so every row binds to the hash approval will
compute (`gate.check_hash`, once per run). Order is fixed:

1. Code checks (layer CODE, no model): the truth compile (`truth.build`, written through
   `truth.to_check_results`), the gate's unresolved links, the copy slop catalogue, sameness
   against the tenant's recent pieces and a generic corpus, length per channel, the PII and
   prompt-injection screen; then per attached media item, keyed on its bytes hash: provenance
   (generated imagery FAILs unless the operator enables it, then always FLAGs), the design lint of
   a template render (written through `media.lint.to_check_results`), the photo check and the
   attestation FLAG of a client photo, and a FAIL for any source the gate cannot cover.
2. Graders (layer GRADER): text graders through the registry, the vision grader on every image.
3. The second reader (layer SECOND_READER), from another model family than the writer.

Graders and the second reader only run when the code checks raised no FAIL (the piece is going
back for repair anyway, so nothing is spent on it). They can only FLAG (see `models.py`).

Attempts: each key (layer, check name) gets the highest attempt it already has for this variant,
across every hash, plus one; so the stack's rows always sit above copy production's. Row ids rise
in stack order and above every existing row of the variant, so a later run's PASS is newer than an
earlier FAIL even within one millisecond of the clock (gate/checks.py orders rows by id).

Repair: when a run FAILs, `repair` gets the piece and the failing rows and returns a new finished
piece (new content, so a new hash). Up to `MAX_REPAIR_LOOPS` loops; the fourth failing run sends
the piece to a person with the failing checks named. Nothing is sent, posted or approved here.

What is fixture-only today: the vision grader (no contract request carries image bytes to a model,
so there is no registry route; `VisionGrader` is implemented only by recorded fixtures), and the
photo measurements for anything but PNG (no image library is a dependency).
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from thallo.checks import models as m
from thallo.checks import photo, text
from thallo.checks.text import Finding
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts.clock import Clock
from thallo.contracts.enums import CheckLayer, CheckVerdict, MediaKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import Brief, CheckResult, Claim, CostEntry, Tenant
from thallo.gate.canonical import (
    LinkResolver,
    MediaItem,
    VariantContent,
    canonical_text,
    find_unresolved_links,
    outgoing_text,
)
from thallo.gate.checks import GENERATED, MEDIA_COVERAGE, PROVENANCE_CHECK, check_hash
from thallo.media.lint import LintReport
from thallo.media.lint import to_check_results as lint_rows
from thallo.truth import CompileFailed, FillError, LibraryError, build, to_check_results

MAX_REPAIR_LOOPS = 3
LINKS = "gate.links"
FILL = "truth.fill"
LIBRARY = "truth.library"
SOURCE = "media.source"
DEFAULT_CAP_EUR = Decimal("0.50")

# Canvas per channel for the photo check (pixels). A brief or template can pass its own.
DEFAULT_CANVAS = photo.Target(1080, 1350, safe=(54.0, 54.0, 54.0, 54.0))


class StackStatus(StrEnum):
    READY_FOR_REVIEW = "ready_for_review"  # no FAIL: flags (if any) wait for the approver
    NEEDS_HUMAN = "needs_human"  # still failing after the repair loops, or no repair possible


@dataclass(frozen=True)
class FinishedPiece:
    """The piece as approval will see it, plus what the checks need to read it honestly."""

    content: VariantContent  # from gate.load_bundle: media carry the bytes hash read from storage
    media_bytes: Mapping[str, bytes] = field(default_factory=dict)  # bytes hash to the bytes read
    lint: Mapping[str, LintReport] = field(default_factory=dict)  # bytes hash to its render's lint
    sources: Sequence[str] = ()  # the source spans the piece may use (untrusted)
    brief: Brief | None = None
    subjects: Mapping[str, photo.Box] = field(default_factory=dict)  # bytes hash to a marked subject
    canvas: photo.Target = DEFAULT_CANVAS
    max_chars: int | None = None


@dataclass(frozen=True)
class Settings:
    """Operator settings. Generated imagery is off in v1: turning it on is an operator decision."""

    allow_generated_media: bool = False
    graders: tuple[m.TextGrader, ...] = m.DEFAULT_GRADERS
    dropped_graders: frozenset[str] = frozenset()  # from the weekly agreement job: flags not written
    second_reader: bool = True
    cost_cap_eur: Decimal = DEFAULT_CAP_EUR
    allowed_terms: tuple[str, ...] = ()
    protected_names: tuple[str, ...] = ()
    extra_slop: tuple[str, ...] = ()
    allowed_contacts: tuple[str, ...] = ()
    voice_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class Context:
    tenant: Tenant
    claims: Mapping[str, Claim]  # truth.library(repo, tenant.id)
    clock: Clock
    resolver: LinkResolver  # repo.resolve_link
    non_links: Collection[str]  # repo.non_link_allowlist
    registry: ConnectionRegistry
    recent_texts: Sequence[str] = ()  # the brand's last pieces, oldest first
    existing: Sequence[CheckResult] = ()  # this variant's rows already written (any hash)
    vision: m.VisionGrader | None = None
    settings: Settings = Settings()
    brand_id: str | None = None
    format: str | None = None


class Repairer(Protocol):
    def repair(self, piece: FinishedPiece, failing: Sequence[CheckResult]) -> FinishedPiece | None: ...


@dataclass(frozen=True)
class Run:
    attempt_hash: str
    rows: tuple[CheckResult, ...]

    @property
    def failing(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.rows if r.verdict is CheckVerdict.FAIL)


@dataclass(frozen=True)
class StackOutcome:
    status: StackStatus
    note: str
    piece: FinishedPiece  # the last piece checked
    runs: tuple[Run, ...]
    cost_entries: tuple[CostEntry, ...]
    spent_eur: Decimal
    dropped: tuple[str, ...] = ()  # graders whose flags were not written, and why

    @property
    def check_results(self) -> tuple[CheckResult, ...]:
        return tuple(r for run in self.runs for r in run.rows)

    @property
    def content_sha256(self) -> str:
        return self.runs[-1].attempt_hash

    def rows(self) -> list[Any]:
        return [*self.check_results, *self.cost_entries]


# ---------------------------------------------------------------- code checks

def _truth(piece: FinishedPiece, ctx: Context, *, variant_id: str, sha: str) -> list[CheckResult | Finding]:
    v = piece.content.variant
    claims = ctx.claims
    allowed = piece.brief.allowed_claim_ids if piece.brief is not None else None
    try:
        built = build(v.text, tenant_id=ctx.tenant.id, language=v.language, claims=claims, clock=ctx.clock,
                      default_language=ctx.tenant.default_language, allowed_claim_ids=allowed,
                      protected_names=ctx.settings.protected_names, allowed_terms=ctx.settings.allowed_terms,
                      tenant_currency=ctx.tenant.currency)
    except CompileFailed as exc:
        return [*to_check_results(exc.result, variant_id=variant_id, content_sha256=sha, clock=ctx.clock),
                Finding(FILL, CheckVerdict.PASS, "not filled: the compile blocked"),
                Finding(LIBRARY, CheckVerdict.PASS)]
    except FillError as exc:
        return [Finding(FILL, CheckVerdict.FAIL, "; ".join(exc.problems)), Finding(LIBRARY, CheckVerdict.PASS)]
    except LibraryError as exc:
        return [Finding(LIBRARY, CheckVerdict.FAIL, f"the claims library is not usable: {exc}"),
                Finding(FILL, CheckVerdict.PASS, "not filled: the library is not usable")]
    rows: list[CheckResult | Finding] = list(to_check_results(built.compiled, variant_id=variant_id,
                                                              content_sha256=sha, clock=ctx.clock))
    if v.rendered_text is None or canonical_text(v.rendered_text) != canonical_text(built.filled.text):
        rows.append(Finding(FILL, CheckVerdict.FAIL, "the rendered text is not what the claims fill to: it was "
                                                     "changed after filling, or never filled"))
    else:
        rows.append(Finding(FILL, CheckVerdict.PASS))
    rows.append(Finding(LIBRARY, CheckVerdict.PASS))
    return rows


def _media(piece: FinishedPiece, ctx: Context, item: MediaItem, content_sha: str) -> list[CheckResult | Finding]:
    sha, source = item.bytes_sha256, item.media.provenance.value
    out: list[CheckResult | Finding] = []
    if source in GENERATED:
        if ctx.settings.allow_generated_media:
            out.append(Finding(PROVENANCE_CHECK, CheckVerdict.FLAG,
                               "generated image, allowed by the operator: check hands, faces, text, logos and "
                               "products; never a generated person shown as a customer", sha))
        else:
            out.append(Finding(PROVENANCE_CHECK, CheckVerdict.FAIL,
                               "generated imagery is off: use the client's photo or a template render", sha))
    else:
        out.append(Finding(PROVENANCE_CHECK, CheckVerdict.PASS, f"source: {source}", sha))
    if source not in MEDIA_COVERAGE:
        out.append(Finding(SOURCE, CheckVerdict.FAIL, f"no check family covers media from {source!r}", sha))
        return out
    if source == "template_render":
        report = piece.lint.get(sha)
        if report is None:
            out.append(Finding("design.request", CheckVerdict.FAIL, "no design lint for these exact bytes", sha))
        else:
            out += lint_rows(report, tenant_id=ctx.tenant.id, variant_id=piece.content.variant.id,
                             content_sha256=content_sha, clock=ctx.clock, media_sha256=sha)
    elif source == "client_photo":
        data = piece.media_bytes.get(sha)
        if data is None:
            out += photo.undecodable("the photo's bytes were not read", media_sha256=sha)
        elif item.media.kind is not MediaKind.IMAGE:
            out += photo.undecodable(f"a {item.media.kind.value} is not a photo", media_sha256=sha)
        else:
            try:
                raster = photo.decode(data, item.media.mime)
            except photo.PhotoUndecodable as exc:
                out += photo.undecodable(str(exc), media_sha256=sha)
            else:
                out += photo.check_photo(raster, piece.canvas, subject=piece.subjects.get(sha), media_sha256=sha)
        out.append(photo.attestation(media_sha256=sha))
    return out


def code_checks(piece: FinishedPiece, ctx: Context, *, sha: str) -> list[CheckResult | Finding]:
    v = piece.content.variant
    outgoing = outgoing_text(v)
    out = _truth(piece, ctx, variant_id=v.id, sha=sha)
    links = find_unresolved_links(outgoing, ctx.resolver, frozenset(ctx.non_links))
    out += [Finding(LINKS, CheckVerdict.FAIL, f"link with no known destination: {u}") for u in links] or [
        Finding(LINKS, CheckVerdict.PASS)]
    out += text.slop(outgoing, v.language, ctx.settings.extra_slop)
    out += text.sameness(outgoing, ctx.recent_texts)
    out += text.length(outgoing, v.channel, max_chars=piece.max_chars)
    out += text.screen(outgoing, allowed_contacts=ctx.settings.allowed_contacts)
    for item in sorted(piece.content.media, key=lambda i: (i.position, i.bytes_sha256)):
        out += _media(piece, ctx, item, sha)
    return out


# ---------------------------------------------------------------- judgment layers

def judgment(piece: FinishedPiece, ctx: Context, ledger: m.Ledger, *, sha: str,
             dropped: list[str]) -> tuple[list[Finding], list[Finding]]:
    """(grader findings, second-reader findings)."""
    v = piece.content.variant
    outgoing = outgoing_text(v)
    scope = {"brand_id": ctx.brand_id, "format": ctx.format}
    brief = None
    if piece.brief is not None:
        b = piece.brief
        brief = f"audience: {b.audience}\njob: {b.job_to_be_done}\nmessage: {b.message}\ncall to action: " \
                f"{b.call_to_action or ''}"
    graders: list[Finding] = []
    for g in ctx.settings.graders:
        if g.name in ctx.settings.dropped_graders:
            if g.name not in dropped:
                dropped.append(g.name)
            continue
        graders += m.run_grader(g, registry=ctx.registry, ledger=ledger, ref=f"checks:{v.id}:{sha[:16]}:{g.name}",
                                text=outgoing, brief=brief, voice_rules=ctx.settings.voice_rules,
                                sources=piece.sources, language=v.language, scope=scope)
    for item in sorted(piece.content.media, key=lambda i: (i.position, i.bytes_sha256)):
        if item.media.kind is not MediaKind.IMAGE:
            continue
        if m.VISION in ctx.settings.dropped_graders:
            if m.VISION not in dropped:
                dropped.append(m.VISION)
            continue
        data = piece.media_bytes.get(item.bytes_sha256)
        if data is None:
            graders.append(Finding(m.VISION, CheckVerdict.FLAG, "the image bytes were not read: look at it yourself",
                                   item.bytes_sha256))
            continue
        graders += m.run_vision(ctx.vision, ledger=ledger, ref=f"checks:{v.id}:{sha[:16]}:vision:{item.bytes_sha256}",
                                image=data, mime=item.media.mime, media_sha256=item.bytes_sha256)
    reader: list[Finding] = []
    if ctx.settings.second_reader:
        reader = m.second_read(registry=ctx.registry, ledger=ledger, ref=f"checks:{v.id}:{sha[:16]}:second_read",
                               text=outgoing, sources=piece.sources, writer_model=v.model_id, language=v.language,
                               scope=scope)
    return graders, reader


# ---------------------------------------------------------------- rows

class _Ids:
    """Row ids that rise in the order rows are made, above every id already written for the
    variant. They stay uuid7 on the clock's millisecond unless the clock is behind an existing row."""

    def __init__(self, clock: Clock, floor: str | None) -> None:
        self.clock = clock
        self.last = int(floor.replace("-", ""), 16) if floor else -1

    def next(self) -> str:
        value = int(new_id(self.clock).replace("-", ""), 16)
        if value <= self.last:
            low = (1 << 62) - 1  # rand_b
            if self.last & low == low:  # rand_b is full: carry into rand_a, keep the variant bits
                value = (((self.last >> 64) + 1) << 64) | (0b10 << 62)
            else:
                value = self.last + 1
        self.last = value
        h = f"{value:032x}"
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _attempts(existing: Sequence[CheckResult], variant_id: str) -> dict[tuple[str, str], int]:
    top: dict[tuple[str, str], int] = {}
    for r in existing:
        if r.variant_id == variant_id:
            key = (r.layer.value, r.check_name)
            top[key] = max(top.get(key, 0), r.attempt)
    return top


def _rows(items: Sequence[tuple[CheckLayer, CheckResult | Finding]], *, tenant_id: str, variant_id: str,
          sha: str, clock: Clock, ids: _Ids, attempts: dict[tuple[str, str], int],
          model_ids: Mapping[str, str | None]) -> list[CheckResult]:
    """One attempt number per key for this run (the key's highest so far plus one), ids in order."""
    run_attempt: dict[tuple[str, str], int] = {}
    out: list[CheckResult] = []
    for given, item in items:
        layer = item.layer if isinstance(item, CheckResult) else given
        if isinstance(item, CheckResult):
            name, verdict, reason, media = item.check_name, item.verdict, item.reason, item.media_sha256
        else:
            name, verdict, reason, media = item.check, item.verdict, item.reason, item.media_sha256
        key = (layer.value, name)
        if key not in run_attempt:
            run_attempt[key] = attempts.get(key, 0) + 1
        out.append(CheckResult(id=ids.next(), created_at=clock.now(), tenant_id=tenant_id, variant_id=variant_id,
                               layer=layer, check_name=name, verdict=verdict, reason=reason,
                               attempt=run_attempt[key], model_id=model_ids.get(name), content_sha256=sha,
                               media_sha256=media))
    attempts.update(run_attempt)
    return out


def check_once(piece: FinishedPiece, ctx: Context, ledger: m.Ledger, *, ids: _Ids,
               attempts: dict[tuple[str, str], int], dropped: list[str]) -> Run:
    """One pass of the stack, in order. Every row is bound to the hash approval will compute."""
    sha = check_hash(piece.content, resolver=ctx.resolver, non_links=ctx.non_links)
    v = piece.content.variant
    items: list[tuple[CheckLayer, CheckResult | Finding]] = [(CheckLayer.CODE, x)
                                                             for x in code_checks(piece, ctx, sha=sha)]
    if not any(x.verdict is CheckVerdict.FAIL for _, x in items):
        graders, reader = judgment(piece, ctx, ledger, sha=sha, dropped=dropped)
        if any(f.verdict is CheckVerdict.FAIL for f in (*graders, *reader)):  # pragma: no cover - guarded above
            raise AssertionError("a grader or the second reader produced a FAIL")
        items += [(CheckLayer.GRADER, f) for f in graders]
        items += [(CheckLayer.SECOND_READER, f) for f in reader]
    rows = _rows(items, tenant_id=ctx.tenant.id, variant_id=v.id, sha=sha, clock=ctx.clock, ids=ids,
                 attempts=attempts, model_ids={})
    return Run(sha, tuple(rows))


def run_stack(piece: FinishedPiece, ctx: Context, *, repair: Repairer | None = None) -> StackOutcome:
    """Check, and repair up to `MAX_REPAIR_LOOPS` times. Nothing is written: pass the outcome to
    `write_stack`. Cost rows are in the outcome even when the run ends early."""
    v = piece.content.variant
    if v.tenant_id != ctx.tenant.id:
        raise ValueError("the piece belongs to another tenant")
    ledger = m.Ledger(ctx.tenant.id, ctx.clock, ctx.settings.cost_cap_eur, v.id)
    existing = [r for r in ctx.existing if r.variant_id == v.id]
    ids = _Ids(ctx.clock, max((r.id for r in existing), default=None))
    attempts = _attempts(existing, v.id)
    dropped: list[str] = []
    runs: list[Run] = []
    loops = 0
    while True:
        run = check_once(piece, ctx, ledger, ids=ids, attempts=attempts, dropped=dropped)
        runs.append(run)
        if not run.failing:
            note = "no check failed" + (f"; {sum(1 for r in run.rows if r.verdict is CheckVerdict.FLAG)} flag(s) for "
                                        "the approver" if any(r.verdict is CheckVerdict.FLAG for r in run.rows) else "")
            status = StackStatus.READY_FOR_REVIEW
            break
        names = sorted({r.check_name for r in run.failing})
        if loops >= MAX_REPAIR_LOOPS or repair is None:
            why = f"after {loops} repair loop(s)" if repair is not None else "with no repair available"
            note = f"sent to a person {why}. Failing check(s): {', '.join(names)}"
            status = StackStatus.NEEDS_HUMAN
            break
        fixed = repair.repair(piece, run.failing)
        drain = getattr(repair, "drain_costs", None)
        if callable(drain):
            ledger.absorb(list(drain()))
        loops += 1
        if fixed is None:
            note = f"sent to a person: the repair gave up on loop {loops}. Failing check(s): {', '.join(names)}"
            status = StackStatus.NEEDS_HUMAN
            break
        if fixed.content.variant.id != v.id or fixed.content.variant.tenant_id != v.tenant_id:
            raise ValueError("a repair must return the same variant")
        piece = fixed
    drops = tuple(f"{name}: dropped by the weekly agreement check, its flags are not written" for name in dropped)
    return StackOutcome(status, note, piece, tuple(runs), tuple(ledger.entries), ledger.spent, drops)


# ---------------------------------------------------------------- writing

def write_stack(repo: Any, outcome: StackOutcome) -> None:
    """Every check row and cost row in one transaction (`repo.conn.transaction()`, as
    `thallo.store.TenantRepo` offers), refused unless every row is the repo's tenant's."""
    conn = getattr(repo, "conn", None)
    if conn is None or not callable(getattr(conn, "transaction", None)):
        raise ValueError("write_stack needs a repo with a transaction; it never writes row by row")
    tenant = getattr(repo, "tenant_id", None)
    if not isinstance(tenant, str) or not tenant:
        raise ValueError("write_stack needs a repo scoped to one tenant")
    rows = outcome.rows()
    if any(str(r.tenant_id).lower() != tenant.lower() for r in rows):
        raise ValueError("outcome rows belong to another tenant than the repo's")
    with conn.transaction():
        for row in rows:
            repo.insert(row)


def with_text(piece: FinishedPiece, *, text: str, rendered_text: str | None) -> FinishedPiece:
    """A repaired piece: the same variant with new text (a repairer's helper)."""
    v = piece.content.variant.model_copy(update={"text": text, "rendered_text": rendered_text})
    return replace(piece, content=replace(piece.content, variant=v))


__all__ = [
    "DEFAULT_CANVAS", "FILL", "LIBRARY", "LINKS", "MAX_REPAIR_LOOPS", "SOURCE", "Context", "FinishedPiece",
    "Repairer", "Run", "Settings", "StackOutcome", "StackStatus", "check_once", "code_checks", "run_stack",
    "with_text", "write_stack",
]
