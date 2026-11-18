"""The review deck: one card per bundle in review, and approve, edit, reject, withdraw.

The card is built from the gate's own loader and functions (`load_bundle`, `build_snapshot`,
`snapshot.payload`, `content_types`, `review_checks`), so it shows exactly what approval will hash:
the root hash, each variant's text as hashed, its media by hash, every open FLAG (each to be
acknowledged by id), and every FAIL and every "not checked" type as blocking with its check name.

Showing a card records its root hash and the time against the session. Approval passes THAT hash
to the gate, which recomputes the bundle and refuses if it changed since it was shown. The client
sends only the acknowledged flag ids and its count of seconds on screen, which is clamped to the
time the server saw since the card was shown. It never sends a hash, a verdict or a tenant.

Edits: `void_bundle_approvals` first, then the edit, in one tenant transaction (gate/AUDIT.md).
The edited variant goes back to DRAFTING with its rendered text cleared, so its hash changes and
every check row about the old content stops counting; the check stack (WP-07) reruns the checks.

  GET  /api/review/bundles
  GET  /api/review/bundles/{id}               the card (records what was shown)
  POST /api/review/bundles/{id}/approve       {acknowledged_flag_ids, seconds_on_screen}
  POST /api/review/bundles/{id}/reject        {reason, note?}
  POST /api/review/bundles/{id}/withdraw      voids a live approval
  POST /api/review/variants/{id}/edit         {text, kind?, reason?}
  GET  /api/media/{id}                        media bytes of this tenant, checked against their hash
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import date

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from api.check_labels import label
from api.context import REVIEWERS, Ctx, member, services
from api.errors import ApiError, conflict, not_found
from api.sessions import Shown
from thallo.contracts.actors import ActorRef, HumanOnlyViolation
from thallo.contracts.enums import ApprovalMethod, EditKind, RejectionReason
from thallo.contracts.ids import is_id, new_id
from thallo.contracts.models import Asset, Brief, Bundle, Edit, Media, Pillar, PlanSlot, Variant
from thallo.contracts.states import (
    BUNDLE,
    VARIANT,
    BundleState,
    ClaimState,
    IllegalTransition,
    PlanSlotState,
    VariantState,
)
from thallo.gate.approvals import ApprovalRefused, approve_bundle_from_repo, void_bundle_approvals
from thallo.gate.canonical import CanonicalFormError, MemoResolver, build_snapshot
from thallo.gate.checks import content_types, review_checks
from thallo.gate.repository import MediaUnreadable, load_bundle
from thallo.store.adapters.blobs import BlobError
from thallo.trust.audit import AuditConflict

log = logging.getLogger("thallo.api.review")
router = APIRouter(prefix="/api")

# Only these types are served inline. SVG and HTML can carry script, so they are never served.
SERVABLE = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "video/mp4", "audio/mpeg",
                      "application/pdf"})
MAX_EDIT = 20_000
RETRIES = 3


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledged_flag_ids: list[str] = Field(default_factory=list, max_length=500)
    seconds_on_screen: float = Field(ge=0, le=86_400)


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: RejectionReason
    note: str | None = Field(default=None, max_length=2_000)


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_EDIT)
    kind: EditKind | None = None
    reason: str | None = Field(default=None, max_length=2_000)


def _id(value: str) -> str:
    if not is_id(value):
        raise not_found()
    return value


# ---------------------------------------------------------------------- the card

def _card(tx, ctx: Ctx, bundle: Bundle) -> dict:  # noqa: ANN001
    t = ctx.tenant_id
    gate = tx.gate
    asset = tx.get(Asset, bundle.asset_id)
    card: dict = {"bundle_id": bundle.id, "state": bundle.state.value, "root_hash": None,
                  "why": asset.selection_reason if asset is not None else None,
                  "reason": None, "headline": None, "slot": None,
                  "variants": [],"flags": [], "blocking": [], "can_approve": False}
    try:
        content = load_bundle(gate, t, bundle)
        snapshot = build_snapshot(content, MemoResolver(lambda url: gate.resolve_link(t, url)),
                                  frozenset(gate.non_link_allowlist(t)))
    except MediaUnreadable:
        card["blocking"].append({"kind": "problem", "variant_id": None, "check_name": "media.readable",
                                 "reason": "a media file could not be read; nothing can be approved",
                                 **label("media.readable")})
        return card
    except CanonicalFormError as exc:
        card["blocking"].append({"kind": "problem", "variant_id": None, "check_name": "gate.canonical_form",
                                 "reason": str(exc), **label("gate.canonical_form")})
        return card
    hashes = snapshot.hash.variants
    types = {vid: content_types(snapshot.payload(vid)) for vid in hashes}
    rows = [r for vid, h in hashes.items() for r in gate.get_check_results(t, vid, h)]
    review = review_checks(rows, tenant_id=t, hashes=hashes, types=types)
    by_id = {r.id: r for r in review.counted}
    card["root_hash"] = snapshot.hash.root
    card["flags"] = [{"id": r.id, "variant_id": r.variant_id, "check_name": r.check_name, "layer": r.layer.value,
                      "reason": r.reason, **label(r.check_name)} for rid in sorted(review.flags) if (r := by_id[rid])]
    card["blocking"] += [{"kind": "fail", "variant_id": r.variant_id, "check_name": r.check_name,
                          "reason": r.reason, **label(r.check_name)}
                         for rid in sorted(review.failing) if (r := by_id[rid])]
    card["blocking"] += [{"kind": "not_checked", "variant_id": vid, "check_name": f"coverage.{kind.value}",
                          "reason": f"no {kind.value} check has run on this content",
                          **label(f"coverage.{kind.value}")}
                         for vid, kinds in review.missing.items() for kind in kinds]
    for vc in content.variants:
        v = vc.variant
        payload = snapshot.payload(v.id)
        media_ids = {(m.position, m.bytes_sha256): m.media.id for m in vc.media}
        card["variants"].append({
            "id": v.id, "channel": v.channel.value, "language": v.language.value, "state": v.state.value,
            "version": v.version, "variant_hash": payload.variant_hash, "text": payload.text, "source_text": v.text,
            "media": [{"id": media_ids.get((m.position, m.sha256)), "position": m.position, "sha256": m.sha256,
                       "kind": m.kind, "mime": m.mime, "alt_text": m.alt_text,
                       "disclosure_label": m.disclosure_label,
                       "url": f"/api/media/{media_ids[(m.position, m.sha256)]}"
                       if (m.position, m.sha256) in media_ids else None} for m in payload.media],
            "links": [{"url": u, "destination": d} for u, d in payload.links],
            "claims": [{"code": c.code, "value": c.value, "unit": c.unit, "state": c.state.value,
                        "source_id": c.source_id} for c in vc.claims],
        })
    card["can_approve"] = (bundle.state is BundleState.IN_REVIEW and not card["blocking"]
                           and all(v["state"] == VariantState.REVIEW.value for v in card["variants"]))
    card["slot"] = _slot(tx, [v["id"] for v in card["variants"]])
    card["headline"] = _headline(card["variants"])
    card["reason"] = _reason(tx, asset, card["slot"], card["variants"])
    return card


# ---------------------------------------------------------------------- plain words for the card
# Read only, from rows the tenant already has. Nothing is invented: a line with no data behind it
# is None and the UI shows nothing in its place.

def _slot(tx, variant_ids: list[str]) -> dict | None:  # noqa: ANN001
    """The earliest live week-plan slot that carries one of these variants."""
    slots = [s for vid in variant_ids for s in tx.list_all(PlanSlot, variant_id=vid)
             if s.state is not PlanSlotState.KILLED]
    if not slots:
        return None
    s = min(slots, key=lambda x: x.starts_at)
    pillar = tx.get(Pillar, s.pillar_id) if s.pillar_id else None
    return {"starts_at": s.starts_at.isoformat(), "day": s.starts_at.date().isoformat(),
            "pillar": pillar.name if pillar is not None else None, "stage": s.stage.value}


def _headline(variants: list[dict]) -> str | None:
    """The first line of the text as it will post, cut at 90 characters."""
    for v in variants:
        line = next((ln.strip() for ln in v["text"].splitlines() if ln.strip()), "")
        if line:
            return line if len(line) <= 90 else line[:89].rstrip(" .,;:") + "…"
    return None


def _reason(tx, asset: Asset | None, slot: dict | None, variants: list[dict]) -> str | None:  # noqa: ANN001
    """One plain line: the slot it fills, else the verified fact it is built on, else who it is for."""
    if slot is not None:
        day = date.fromisoformat(slot["day"]).strftime("%A")
        return f"Fills {day}'s {slot['pillar']} slot." if slot["pillar"] else f"Fills {day}'s slot."
    fact = next((c for v in variants for c in v["claims"] if c["state"] == ClaimState.VERIFIED.value), None)
    if fact is not None:
        unit = f" {fact['unit']}" if fact["unit"] else ""
        return f"Built on a fact you verified: {fact['value']}{unit}."
    brief = tx.get(Brief, asset.brief_id) if asset is not None else None
    if brief is not None and brief.audience.strip():
        who = brief.audience.strip().rstrip(".")
        return f"Written for {who[0].lower()}{who[1:]}."
    return None


def _status(card: dict) -> str:
    """ready, flag (a person has to read a flag first) or blocked."""
    if card["blocking"] or not card["can_approve"]:
        return "blocked"
    return "flag" if card["flags"] else "ready"


@router.get("/review/bundles")
def list_bundles(request: Request) -> dict:
    ctx = member(request, REVIEWERS)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        rows = tx.list_all(Bundle, state=BundleState.IN_REVIEW)
        out = []
        for b in rows:
            variants = tx.list_all(Variant, bundle_id=b.id)
            card = _card(tx, ctx, b)  # read only: showing a card is recorded by get_card alone
            thumb = next((m["url"] for v in card["variants"] for m in v["media"]
                          if m["url"] and m["mime"].startswith("image/")), None)
            out.append({"bundle_id": b.id, "created_at": b.created_at.isoformat(), "state": b.state.value,
                        "channels": sorted({v.channel.value for v in variants}), "variant_count": len(variants),
                        "headline": card["headline"], "thumbnail_url": thumb, "slot": card["slot"],
                        "status": _status(card), "flag_count": len(card["flags"]),
                        "blocking_count": len(card["blocking"])})
    return {"bundles": out}


@router.get("/review/bundles/{bundle_id}")
def get_card(bundle_id: str, request: Request) -> dict:
    ctx = member(request, REVIEWERS)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        bundle = tx.get(Bundle, _id(bundle_id))
        if bundle is None:
            raise not_found()
        card = _card(tx, ctx, bundle)
    shown = dict(ctx.session.shown)
    shown[bundle.id] = Shown(root_hash=card["root_hash"], at=svc.clock.now())
    svc.sessions.save(replace(ctx.session, shown=shown))
    return card


# ---------------------------------------------------------------------- approve

@router.post("/review/bundles/{bundle_id}/approve")
def approve(bundle_id: str, body: ApproveBody, request: Request) -> dict:
    ctx = member(request, REVIEWERS, unsafe=True)
    svc = services(request)
    shown = ctx.session.shown.get(_id(bundle_id))
    if shown is None or shown.root_hash is None:
        raise conflict("open the card before approving it")
    if svc.signer is None:
        raise ApiError(503, "not_configured", "approvals are switched off: no gate signing key is configured")
    elapsed = max(0.0, (svc.clock.now() - shown.at).total_seconds())
    seconds = min(float(body.seconds_on_screen), elapsed)
    for attempt in range(RETRIES):
        try:
            with svc.backend.tenant(ctx.tenant_id) as tx:
                change = approve_bundle_from_repo(
                    tx.gate, tenant_id=ctx.tenant_id, bundle_id=bundle_id, actor=ctx.human(),
                    verifier=svc.verifier, shown_hash=shown.root_hash, method=ApprovalMethod.PER_BUNDLE,
                    seconds_on_screen=seconds, clock=svc.clock, audit=tx.audit, signer=svc.signer,
                    acknowledged_flag_ids=tuple(body.acknowledged_flag_ids))
                tx.write(change)
            break
        except AuditConflict:
            if attempt == RETRIES - 1:
                raise conflict("someone else changed this tenant's log at the same moment; try again") from None
        except HumanOnlyViolation:
            raise ApiError(403, "not_human", "approval needs a live, signed-in person") from None
        except ApprovalRefused as exc:
            if str(exc) == "bundle not found":
                raise not_found() from None
            raise conflict(f"not approved: {exc}") from None
    remaining = {k: v for k, v in ctx.session.shown.items() if k != bundle_id}
    svc.sessions.save(replace(ctx.session, shown=remaining))
    assert change.new_approval is not None
    return {"status": "approved", "approval_id": change.new_approval.id, "root_hash": change.new_approval.content_hash,
            "expires_at": change.new_approval.expires_at.isoformat(), "seconds_on_screen": seconds}


# ---------------------------------------------------------------------- void, then change

def _void(tx, svc, ctx: Ctx, bundle: Bundle, reason: str) -> Bundle:  # noqa: ANN001
    """Void every live approval of the bundle and write that change. Returns the bundle as it is now."""
    if svc.signer is None:
        raise ApiError(503, "not_configured", "no gate signing key is configured; nothing can change after review")
    change = void_bundle_approvals(bundle, tx.gate.get_variants(ctx.tenant_id, bundle.id),
                                   tx.gate.get_approvals(ctx.tenant_id, bundle.id), reason=reason,
                                   actor=ActorRef.of(ctx.human()), clock=svc.clock, audit=tx.audit, signer=svc.signer)
    tx.write(change)
    return change.bundle


def _with_tx(request: Request, ctx: Ctx, fn):  # noqa: ANN001, ANN202
    svc = services(request)
    for attempt in range(RETRIES):
        try:
            with svc.backend.tenant(ctx.tenant_id) as tx:
                return fn(tx, svc)
        except AuditConflict:
            if attempt == RETRIES - 1:
                raise conflict("someone else changed this tenant's log at the same moment; try again") from None
        except IllegalTransition:
            raise conflict("this piece is not in a state that allows that") from None
        except ApprovalRefused as exc:
            raise conflict(str(exc)) from None
    raise conflict("try again")


@router.post("/review/variants/{variant_id}/edit")
def edit_variant(variant_id: str, body: EditBody, request: Request) -> dict:
    ctx = member(request, REVIEWERS, unsafe=True)
    _id(variant_id)

    def run(tx, svc) -> dict:  # noqa: ANN001
        variant = tx.get(Variant, variant_id)
        bundle = tx.get(Bundle, variant.bundle_id) if variant is not None else None
        if variant is None or bundle is None:
            raise not_found()
        if bundle.state is BundleState.REJECTED or variant.state in (VariantState.PUBLISHED, VariantState.REJECTED):
            raise conflict("this piece can no longer be edited")
        bundle = _void(tx, svc, ctx, bundle, "edited in review")  # first, in the same transaction
        current = tx.get(Variant, variant_id)
        if current.state is not VariantState.DRAFTING:
            VARIANT.check(current.state, VariantState.DRAFTING)
        tx.update(Variant, variant_id, text=body.text, rendered_text=None, version=current.version + 1,
                  state=VariantState.DRAFTING, content_sha256=None)
        if bundle.state is not BundleState.OPEN:
            BUNDLE.check(bundle.state, BundleState.OPEN)
            tx.update(Bundle, bundle.id, state=BundleState.OPEN, root_hash=None)
        now = svc.clock.now()
        tx.insert(Edit(id=new_id(svc.clock), created_at=now, tenant_id=ctx.tenant_id, variant_id=variant_id,
                       editor_id=ctx.user.id, before_text=current.text, after_text=body.text, kind=body.kind,
                       reason=body.reason, counts_as_defect=body.kind is not None and body.kind is not EditKind.TYPO))
        return {"status": "edited", "variant_id": variant_id, "version": current.version + 1,
                "state": VariantState.DRAFTING.value,
                "message": "Saved. The checks run again before this piece comes back for review."}

    return _with_tx(request, ctx, run)


@router.post("/review/bundles/{bundle_id}/reject")
def reject(bundle_id: str, body: RejectBody, request: Request) -> dict:
    ctx = member(request, REVIEWERS, unsafe=True)
    _id(bundle_id)

    def run(tx, svc) -> dict:  # noqa: ANN001
        bundle = tx.get(Bundle, bundle_id)
        if bundle is None:
            raise not_found()
        BUNDLE.check(bundle.state, BundleState.REJECTED)
        bundle = _void(tx, svc, ctx, bundle, f"rejected: {body.reason.value}")
        now = svc.clock.now()
        for v in tx.list_all(Variant, bundle_id=bundle_id):
            if v.state in (VariantState.REVIEW, VariantState.ON_HOLD):
                tx.update(Variant, v.id, state=VariantState.REJECTED)
            tx.insert(Edit(id=new_id(svc.clock), created_at=now, tenant_id=ctx.tenant_id, variant_id=v.id,
                           editor_id=ctx.user.id, before_text=v.text, after_text=v.text, rejection_reason=body.reason,
                           reason=body.note, counts_as_defect=True))
        tx.update(Bundle, bundle_id, state=BundleState.REJECTED)
        return {"status": "rejected", "bundle_id": bundle_id, "reason": body.reason.value}

    return _with_tx(request, ctx, run)


@router.post("/review/bundles/{bundle_id}/withdraw")
def withdraw(bundle_id: str, request: Request) -> dict:
    ctx = member(request, REVIEWERS, unsafe=True)
    _id(bundle_id)

    def run(tx, svc) -> dict:  # noqa: ANN001
        bundle = tx.get(Bundle, bundle_id)
        if bundle is None:
            raise not_found()
        if bundle.state is not BundleState.APPROVED:
            raise conflict("only an approved bundle can be withdrawn")
        after = _void(tx, svc, ctx, bundle, "withdrawn by the approver")
        return {"status": "withdrawn", "bundle_id": bundle_id, "state": after.state.value}

    return _with_tx(request, ctx, run)


# ---------------------------------------------------------------------- media

@router.get("/media/{media_id}")
def media(media_id: str, request: Request) -> Response:
    ctx = member(request, REVIEWERS)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        row = tx.get(Media, _id(media_id))
    if row is None or row.tenant_id != ctx.tenant_id:
        raise not_found()
    if row.mime not in SERVABLE:
        raise ApiError(415, "unsupported_media", "this media type is not shown in the browser")
    try:
        data = svc.media.read(ctx.tenant_id, row.storage_uri)
    except BlobError:
        raise not_found() from None
    if hashlib.sha256(data).hexdigest() != row.sha256:
        raise conflict("the stored file no longer matches its recorded hash")
    return Response(content=data, media_type=row.mime, headers={
        "Cache-Control": "private, no-store", "Content-Disposition": "inline",
        "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff"})


__all__ = ["router"]
