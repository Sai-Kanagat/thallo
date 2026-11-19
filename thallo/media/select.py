"""Which media a piece uses, and which requests to make when it has none (WP-11).

Plain rules, no model:

1. Real material first: the client's own photo, graphic or clip on record.
2. Then a template render (images: the brand template; video: template frames to MP4, or local
   cuts and captions of a real clip).
3. Generated media never by default. A generated request is planned only when the operator switch
   is on (THALLO_ALLOW_GENERATED_MEDIA=1, environment only) AND the format allows it, and every
   generated file is FLAGged downstream (`media_file.flagged`, the check stack's FLAG). A
   generated image is only ever a background or an illustration: claims ride on real photos or
   typeset text, so a generated file is never the piece's main image when a real one exists.

A review card can override the choice for one piece (`piece_override`): it adds a piece-scoped
connection, which the registry tries before the tenant's own, with the manual floor still last.
Asking for a generating provider with the switch off is refused here, and the provider itself
refuses too.

Voiceover is per brief: only a brief for a video channel gets a voiceover request, and its chain
defaults to "record your own" (the manual connection) unless the tenant set another one.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from thallo.connections.errors import InvalidConnection
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, Channel, Language, MediaKind, MediaProvenance
from thallo.contracts.models import Brief, Connection, Media

SWITCH_ENV = "THALLO_ALLOW_GENERATED_MEDIA"
GENERATING = frozenset({"generated_hosted", "generated_local"})  # provider names that make new imagery
VIDEO_CHANNELS = frozenset({Channel.INSTAGRAM, Channel.FACEBOOK, Channel.YOUTUBE, Channel.LINKEDIN_COMPANY,
                            Channel.LINKEDIN_PERSONAL})
VIDEO_FRAME = "video_frame-01"
_RANK = {MediaProvenance.CLIENT_PHOTO: 0, MediaProvenance.CLIENT_GRAPHIC: 1, MediaProvenance.CLIENT_CLIP: 0,
         MediaProvenance.PERSON_FILE: 2, MediaProvenance.TEMPLATE_RENDER: 3,
         MediaProvenance.GENERATED_BACKGROUND: 8, MediaProvenance.GENERATED: 9}
REAL = frozenset({MediaProvenance.CLIENT_PHOTO, MediaProvenance.CLIENT_GRAPHIC, MediaProvenance.CLIENT_CLIP,
                  MediaProvenance.PERSON_FILE})
GENERATED = frozenset({MediaProvenance.GENERATED, MediaProvenance.GENERATED_BACKGROUND})


def generation_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """The operator switch. Off unless the environment says exactly "1"."""
    return (os.environ if environ is None else environ).get(SWITCH_ENV) == "1"


@dataclass(frozen=True)
class Plan:
    """`use`: media already on record, best first. `requests`: what to run, in order, when
    `use` is empty (or to make the frame around a photo). `flagged`: a generated file is in it."""

    use: tuple[Media, ...] = ()
    requests: tuple[c.Request, ...] = ()
    flagged: bool = False
    reasons: tuple[str, ...] = field(default=())


def rank(candidates: Sequence[Media], *, kind: MediaKind, allow_generated: bool = False) -> list[Media]:
    """Real files first, then template renders; generated ones only when allowed, and last."""
    keep = [m for m in candidates if m.kind is kind and (allow_generated or m.provenance not in GENERATED)]
    return sorted(keep, key=lambda m: (_RANK.get(m.provenance, 5), m.created_at, m.id))


def image_plan(*, tenant_id: str, width: int, height: int, candidates: Sequence[Media] = (),
               template_ref: str | None = None, slots: Mapping[str, str] | None = None,
               format_allows_generated: bool = False, prompt: str | None = None,
               environ: Mapping[str, str] | None = None, key: str = "piece") -> Plan:
    allowed = generation_allowed(environ) and format_allows_generated
    picked = rank(candidates, kind=MediaKind.IMAGE, allow_generated=False)
    reasons: list[str] = []
    real = [m for m in picked if m.provenance in REAL]
    if real:
        reasons.append(f"using the client's own {real[0].provenance.value}")
        return Plan(use=tuple(real), reasons=tuple(reasons))
    requests: list[c.Request] = []
    if template_ref:
        requests.append(c.ImageRequest(tenant_id=tenant_id, purpose="template_render", template_ref=template_ref,
                                       slots=dict(slots or {}), width=width, height=height,
                                       idempotency_key=f"{key}:image:template"))
        reasons.append("no client photo on record: a template render")
    renders = [m for m in picked if m.provenance is MediaProvenance.TEMPLATE_RENDER]
    if allowed and prompt:
        requests.append(c.ImageRequest(tenant_id=tenant_id, purpose="background", prompt=prompt, width=width,
                                       height=height, idempotency_key=f"{key}:image:background"))
        reasons.append("generated background allowed by the operator and the format: FLAGged")
    elif prompt:
        reasons.append("generated imagery is off (operator switch or format): not requested")
    return Plan(use=tuple(renders), requests=tuple(requests), flagged=allowed and bool(prompt),
                reasons=tuple(reasons))


def video_plan(*, tenant_id: str, width: int, height: int, candidates: Sequence[Media] = (),
               script: str | None = None, duration_ms: int | None = None, template_ref: str = VIDEO_FRAME,
               format_allows_generated: bool = False, environ: Mapping[str, str] | None = None,
               key: str = "piece") -> Plan:
    """Local cuts of real clips first (captioned when there is a script), then template frames
    to video, then, only behind the switch and the format, a generated clip."""
    clips = [m for m in rank(candidates, kind=MediaKind.VIDEO) if m.provenance in REAL]
    reasons: list[str] = []
    if clips:
        first = clips[0]
        action = "caption" if script else "cut"
        req = c.VideoRequest(tenant_id=tenant_id, action=action, input_sha256=(first.sha256,), script=script,
                             width=width, height=height, duration_ms=duration_ms,
                             idempotency_key=f"{key}:video:{action}")
        reasons.append(f"a local {action} of the client's own clip")
        return Plan(use=(first,), requests=(req,), reasons=tuple(reasons))
    requests: list[c.Request] = []
    if script:
        requests.append(c.VideoRequest(tenant_id=tenant_id, action="template_render", script=script,
                                       template_ref=template_ref, width=width, height=height,
                                       duration_ms=duration_ms, idempotency_key=f"{key}:video:template"))
        reasons.append("no client clip on record: template frames rendered to video")
    allowed = generation_allowed(environ) and format_allows_generated
    if allowed and script:
        requests.append(c.VideoRequest(tenant_id=tenant_id, action="generate", script=script, width=width,
                                       height=height, duration_ms=duration_ms,
                                       idempotency_key=f"{key}:video:generate"))
        reasons.append("generated clip allowed by the operator and the format: FLAGged")
    return Plan(requests=tuple(requests), flagged=allowed and bool(script), reasons=tuple(reasons))


def voiceover_request(brief: Brief, *, voice: str = "own", key: str = "piece") -> c.VoiceoverRequest | None:
    """A voiceover only for a brief that goes to a video channel. The text is the brief's message;
    the chain decides who speaks it (by default the client, recording it themselves)."""
    if not any(ch in VIDEO_CHANNELS for ch in brief.channels):
        return None
    return c.VoiceoverRequest(tenant_id=brief.tenant_id, text=brief.message, language=Language(brief.language),
                              voice=voice, idempotency_key=f"{key}:voiceover")


def piece_override(registry: ConnectionRegistry, *, tenant_id: str, piece_id: str, capability: Capability,
                   provider: str, model: str | None = None, settings: dict | None = None,
                   environ: Mapping[str, str] | None = None) -> Connection:
    """"Use my upload", "render locally", "regenerate with model X" from the review card: a
    piece-scoped connection tried first for this piece only. The choice and its cost then land on
    the `media_file` row through `ingest_result(..., piece_id=piece_id)`."""
    if capability not in (Capability.IMAGE, Capability.VIDEO, Capability.VOICEOVER):
        raise InvalidConnection("piece overrides here are for image, video and voiceover")
    if provider in GENERATING and not generation_allowed(environ):
        raise InvalidConnection("generated media is switched off by the operator")
    for row in registry.connections.list_for_tenant(tenant_id):
        if (row.capability is capability and row.scope == c.OverrideScope.PIECE.value and row.scope_ref == piece_id
                and row.provider == provider):
            return row
    return registry.add(tenant_id, capability, provider, scope=c.OverrideScope.PIECE, scope_ref=piece_id,
                        model=model, settings=settings, priority=0)


__all__ = ["GENERATING", "SWITCH_ENV", "Plan", "generation_allowed", "image_plan", "piece_override", "rank",
           "video_plan", "voiceover_request"]
