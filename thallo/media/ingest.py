"""Turning a file into a `media` row: client uploads and provider results (WP-11).

Every path in here takes its provenance from `ingest_provenance(served)` (`thallo/checks/origin.py`):
the source that produced the bytes decides the label, never the caller. Each media row gets one
`media_file` row (migration 0029) in the same transaction: the connection, mode, model, cost,
stock licence, disclosure label, FLAG and the scope of the choice (a per-piece override is
scope 'piece').

Client upload, the main image source:
- an allowlist of types, a size cap, a pixel cap; the bytes must match the declared type;
- images are decoded and re-encoded with Pillow (orientation applied first, then every metadata
  block dropped: EXIF with GPS, XMP, ICC, comments); videos are re-encoded by ffmpeg with
  `-map_metadata -1` and without data or subtitle streams, so no location or device tag survives;
- the clean bytes are stored content-addressed in the tenant's folder, and an identical file
  already on record is reused (a different source for the same bytes is refused);
- the run goes down the tenant's chain like any other request, so the manual connection that
  served it is what `ingest_provenance` sees. An upload that some other connection claims is
  refused: only a person's own file is an upload.

Provider results: the bytes are read back through the tenant's blob store, re-hashed against the
result, copied into the tenant's folder under their hash, and written with the provenance the
served connection implies. Generated or materially altered media must carry the disclosure label
of the tenant's region policy; with no label on record it is refused, never stored unlabelled.
A request that asked for a real person (people allowed, a real voice) must name a consent row of
this tenant with the LIKENESS or VOICE scope, or nothing is stored.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from thallo.checks.origin import ingest_provenance
from thallo.connections.registry import Served
from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import ConnectionMode, ConsentScope, MediaKind, MediaProvenance
from thallo.contracts.ids import Id, new_id
from thallo.contracts.models import Consent, Media, RegionPolicy, TenantRow

IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
VIDEO_MIMES = frozenset({"video/mp4", "video/quicktime", "video/webm"})
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_VIDEO_BYTES = 60 * 1024 * 1024  # under the media store's 64 MB read limit, after re-encoding too
MAX_PIXELS = 40_000_000
GENERATED = frozenset({MediaProvenance.GENERATED, MediaProvenance.GENERATED_BACKGROUND})
FFMPEG_ENV = "THALLO_FFMPEG"
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "video/mp4": "mp4",
        "application/pdf": "pdf", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/mp4": "m4a"}


class IngestError(ValueError):
    """Refused input or result; the message is safe to show the person."""


class MediaFile(TenantRow):
    """Migration 0029: what made a media file, its cost and its label. One per media row."""

    __table__ = "media_file"
    __unique__ = (("tenant_id", "media_id"),)
    media_id: Id
    connection_id: Id | None = None
    provider: str = Field(min_length=1, max_length=100)
    mode: ConnectionMode
    model: str | None = None
    cost_eur: Decimal = Field(ge=0)
    licence: str | None = None
    disclosure_label: str | None = None
    flagged: bool = False
    choice_scope: c.OverrideScope = c.OverrideScope.TENANT
    choice_ref: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class Rows(Protocol):
    def get(self, model: type, row_id: str) -> Any: ...
    def list_all(self, model: type, **where: Any) -> list: ...
    def insert(self, row: Any) -> Any: ...


class Blobs(Protocol):
    def write(self, tenant_id: str, name: str, data: bytes) -> str: ...
    def read(self, tenant_id: str, uri: str) -> bytes: ...


class Runner(Protocol):
    """`ConnectionRegistry.run`."""

    def run(self, tenant_id: str, request: c.Request, *, brand_id: str | None = None,
            format: str | None = None, piece_id: str | None = None) -> Served: ...  # noqa: A002


@dataclass(frozen=True)
class Ingested:
    media: Media
    file: MediaFile | None  # None when an identical file was already on record
    reused: bool = False


# ====================================================================== cleaning uploads

def sniff(data: bytes) -> str | None:
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4" if head[8:10] != b"qt" else "video/quicktime"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None


def _declared(mime: str) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def clean_image(data: bytes, mime: str) -> tuple[bytes, str, int, int]:
    """(clean bytes, mime, width, height). PNG stays PNG (graphics keep their edges); JPEG and
    WebP become JPEG. Nothing of the original container survives: only decoded pixels are saved."""
    from PIL import Image, ImageOps

    m = _declared(mime)
    if m not in IMAGE_MIMES:
        raise IngestError(f"this kind of image ({m or 'unknown'}) is not accepted; use JPEG, PNG or WebP")
    if not data:
        raise IngestError("the file is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise IngestError(f"the image is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    if sniff(data) != m:
        raise IngestError("the file's contents do not match its type")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise IngestError("the image has too many pixels")
            probe.verify()
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            img = ImageOps.exif_transpose(opened)
            if m == "image/png":
                mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
                out_mime, fmt = "image/png", "PNG"
            else:
                mode, out_mime, fmt = "RGB", "image/jpeg", "JPEG"
            pixels = img.convert(mode)
            clean = Image.new(mode, pixels.size)
            clean.paste(pixels)  # a fresh image: no info, exif, icc or xmp carried over
            buf = io.BytesIO()
            if fmt == "JPEG":
                clean.save(buf, fmt, quality=90, optimize=True)
            else:
                clean.save(buf, fmt, optimize=True)
            return buf.getvalue(), out_mime, clean.width, clean.height
    except IngestError:
        raise
    except Exception as exc:  # noqa: BLE001 - any decoder failure is a refused file
        raise IngestError(f"the image could not be read ({type(exc).__name__})") from None


def find_ffmpeg(environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    raw = env.get(FFMPEG_ENV) or shutil.which("ffmpeg")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() and p.is_file() and os.access(p, os.X_OK) else None


def clean_video(data: bytes, mime: str, *, ffmpeg: Path | None = None) -> tuple[bytes, str]:
    """Re-encode to H.264/AAC MP4 with every metadata block dropped. Needs ffmpeg on the server;
    without it a clip cannot be cleaned and is refused (the person can still be asked again)."""
    m = _declared(mime)
    if m not in VIDEO_MIMES:
        raise IngestError(f"this kind of video ({m or 'unknown'}) is not accepted; use MP4, MOV or WebM")
    if not data:
        raise IngestError("the file is empty")
    if len(data) > MAX_VIDEO_BYTES:
        raise IngestError(f"the video is over {MAX_VIDEO_BYTES // (1024 * 1024)} MB")
    iso = {"video/mp4", "video/quicktime"}
    if sniff(data) not in (iso if m in iso else {m}):
        raise IngestError("the file's contents do not match its type")
    binary = ffmpeg or find_ffmpeg()
    if binary is None:
        raise IngestError("video uploads need ffmpeg on the server to remove location data; none is installed")
    with tempfile.TemporaryDirectory(prefix="thallo-clean-") as tmp:
        src, dst = Path(tmp) / "in", Path(tmp) / "out.mp4"
        src.write_bytes(data)
        argv = [str(binary), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
                "-dn", "-sn", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-fflags", "+bitexact",
                "-flags:v", "+bitexact", "-flags:a", "+bitexact", str(dst)]
        try:
            subprocess.run(argv, check=True, capture_output=True, timeout=600)  # noqa: S603 - argv list
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            raise IngestError("the video could not be read") from None
        out = dst.read_bytes()
    if len(out) > MAX_VIDEO_BYTES:
        raise IngestError("the cleaned video is too large")
    return out, "video/mp4"


def probe_size(data: bytes) -> tuple[int, int]:
    """Width and height of an MP4's first video track, read from its tkhd box (16.16 fixed point)."""
    i = data.find(b"tkhd")
    while i != -1:
        version = data[i + 4]
        off = i + 4 + (96 if version == 1 else 84) - 8
        w, h = int.from_bytes(data[off:off + 4], "big") >> 16, int.from_bytes(data[off + 4:off + 8], "big") >> 16
        if w and h:
            return w, h
        i = data.find(b"tkhd", i + 4)
    return 0, 0


# ====================================================================== shared writing

def blob_name(sha: str, mime: str) -> str:
    return f"media-{sha}.{_EXT.get(mime, 'bin')}"


def disclosure_label(rows: Rows, tenant_id: str, region_code: str | None = None) -> str | None:
    """The label the tenant's region policy requires: its newest version with a label, for the
    region asked (or any region when none is named)."""
    policies = [p for p in rows.list_all(RegionPolicy) if p.tenant_id == tenant_id and p.disclosure_label
                and (region_code is None or p.region_code == region_code)]
    if not policies:
        return None
    return max(policies, key=lambda p: (p.version, p.effective_from)).disclosure_label


def check_consent(rows: Rows, tenant_id: str, consent_id: str | None, scope: ConsentScope) -> str:
    if not consent_id:
        raise IngestError(f"a {scope.value} consent record is needed and none was named")
    consent = rows.get(Consent, consent_id)
    if consent is None or consent.tenant_id != tenant_id or scope not in consent.scopes:
        raise IngestError(f"no {scope.value} consent of this tenant has that id")
    return consent.id


def _scope(scope: Mapping[str, str | None]) -> tuple[c.OverrideScope, str | None]:
    for key, s in (("piece_id", c.OverrideScope.PIECE), ("format", c.OverrideScope.FORMAT),
                   ("brand_id", c.OverrideScope.BRAND)):
        if scope.get(key):
            return s, scope[key]
    return c.OverrideScope.TENANT, None


def _store(rows: Rows, blobs: Blobs, *, tenant_id: str, data: bytes, mime: str, kind: MediaKind,
           provenance: MediaProvenance, served: Served, clock: Clock, width: int | None, height: int | None,
           duration_ms: int | None = None, alt_text: str | None = None, consent_id: str | None = None,
           generation_prompt: str | None = None, label: str | None = None, voice: bool = False,
           choice: tuple[c.OverrideScope, str | None] = (c.OverrideScope.TENANT, None)) -> Ingested:
    sha = hashlib.sha256(data).hexdigest()
    for existing in rows.list_all(Media, sha256=sha):
        if existing.tenant_id != tenant_id:
            continue
        if existing.provenance is not provenance:
            raise IngestError(f"these exact bytes are already on record as {existing.provenance.value}")
        return Ingested(existing, None, reused=True)
    uri = blobs.write(tenant_id, blob_name(sha, mime), data)
    if hashlib.sha256(blobs.read(tenant_id, uri)).hexdigest() != sha:
        raise IngestError("the stored file does not hash to the bytes written")
    res = served.result
    model = res.provenance.model
    media = Media(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id, kind=kind, sha256=sha, mime=mime,
                  size_bytes=len(data), storage_uri=uri, provenance=provenance, connection_id=served.connection_id,
                  generation_prompt=generation_prompt, consent_id=consent_id, alt_text=alt_text,
                  disclosure_label=label, width=width, height=height, duration_ms=duration_ms,
                  has_voice_track=voice, model_id=model if provenance in GENERATED else None)
    media = rows.insert(media)
    settings = dict(res.provenance.settings)
    licence = settings.get("licence") if isinstance(settings.get("licence"), str) else None
    file = MediaFile(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id, media_id=media.id,
                     connection_id=served.connection_id, provider=served.provider, mode=served.mode, model=model,
                     cost_eur=res.cost_eur, licence=licence, disclosure_label=label,
                     flagged=provenance in GENERATED, choice_scope=choice[0], choice_ref=choice[1],
                     settings=_json_safe(settings))
    return Ingested(media, rows.insert(file))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ====================================================================== client upload

def ingest_upload(rows: Rows, blobs: Blobs, runner: Runner, *, tenant_id: str, data: bytes, mime: str,
                  clock: Clock, alt_text: str | None = None, people_in_shot: bool = False,
                  consent_id: str | None = None, ffmpeg: Path | None = None, **scope: str | None) -> Ingested:
    """A person's own photo, graphic or clip. `scope` (piece_id, format, brand_id) is the review
    card's override, passed to the chain so a per-piece manual connection is honoured."""
    m = _declared(mime)
    if m in IMAGE_MIMES:
        clean, out_mime, w, h = clean_image(data, m)
        kind = MediaKind.IMAGE
        request: c.Request = c.ImageRequest(tenant_id=tenant_id, purpose="upload", width=w, height=h,
                                            idempotency_key=f"upload:{hashlib.sha256(clean).hexdigest()}")
    elif m in VIDEO_MIMES:
        clean, out_mime = clean_video(data, m, ffmpeg=ffmpeg)
        w, h = probe_size(clean)
        kind = MediaKind.VIDEO
        request = c.VideoRequest(tenant_id=tenant_id, action="upload", width=w or 1, height=h or 1,
                                 idempotency_key=f"upload:{hashlib.sha256(clean).hexdigest()}")
    else:
        raise IngestError(f"this kind of file ({m or 'unknown'}) is not accepted here")
    consent = check_consent(rows, tenant_id, consent_id, ConsentScope.LIKENESS) if people_in_shot else None
    served = runner.run(tenant_id, request, **scope)
    if served.mode is not ConnectionMode.MANUAL:
        raise IngestError(f"an upload must be served by a manual connection, not {served.provider}")
    provenance = ingest_provenance(served)
    return _store(rows, blobs, tenant_id=tenant_id, data=clean, mime=out_mime, kind=kind, provenance=provenance,
                  served=served, clock=clock, width=w or None, height=h or None, alt_text=alt_text,
                  consent_id=consent, voice=kind is MediaKind.VIDEO and _has_audio(clean),
                  choice=_scope(scope))


def _has_audio(mp4: bytes) -> bool:
    """An audio track: the cleaned file is written with faststart, so its moov box (holding each
    track's handler type) comes before the media data."""
    head = mp4[:mp4.find(b"mdat")] if b"mdat" in mp4 else mp4
    return b"soun" in head


# ====================================================================== provider results

def ingest_result(rows: Rows, blobs: Blobs, served: Served, request: c.Request, *, clock: Clock,
                  alt_text: str | None = None, region_code: str | None = None,
                  **scope: str | None) -> Ingested:
    """Store what a provider made. `served` is what `ConnectionRegistry.run` returned for
    `request`; `scope` is the same override the run was given."""
    res = served.result
    if res.status != "done":
        raise IngestError("the connection prepared a task for a person; upload their file instead")
    out = res.output
    if not isinstance(out, c.MediaResult):
        raise IngestError("the result is not a media file")
    tenant_id = request.tenant_id
    try:
        data = blobs.read(tenant_id, out.storage_uri)
    except Exception:  # noqa: BLE001 - BlobError and OS errors alike: nothing is stored
        raise IngestError("the provider's file is not readable from the tenant's folder") from None
    if hashlib.sha256(data).hexdigest() != out.sha256:
        raise IngestError("the provider's file does not hash to what it reported")
    provenance = ingest_provenance(served)
    consent: str | None = None
    if getattr(request, "allow_generated_people", False):
        consent = check_consent(rows, tenant_id, request.consent_id, ConsentScope.LIKENESS)  # type: ignore[attr-defined]
    voice = False
    if isinstance(request, c.VoiceoverRequest):
        voice = request.is_real_person_voice
        if voice:
            consent = check_consent(rows, tenant_id, request.consent_id, ConsentScope.VOICE)
    label = None
    if provenance in GENERATED or out.disclosure_required:
        label = disclosure_label(rows, tenant_id, region_code)
        if label is None:
            raise IngestError("generated or altered media needs the disclosure label of the region policy, "
                              "and the tenant has none on record")
    prompt = getattr(request, "prompt", None) if provenance in GENERATED else None
    return _store(rows, blobs, tenant_id=tenant_id, data=data, mime=out.mime, kind=out.kind, provenance=provenance,
                  served=served, clock=clock, width=out.width, height=out.height, duration_ms=out.duration_ms,
                  alt_text=alt_text, consent_id=consent, generation_prompt=prompt, label=label, voice=voice,
                  choice=_scope(scope))


__all__ = ["GENERATED", "IMAGE_MIMES", "VIDEO_MIMES", "Ingested", "IngestError", "MediaFile", "check_consent",
           "clean_image", "clean_video", "disclosure_label", "find_ffmpeg", "ingest_result", "ingest_upload",
           "probe_size", "sniff"]
