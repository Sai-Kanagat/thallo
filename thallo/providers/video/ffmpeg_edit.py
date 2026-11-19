"""Local cuts and captions of a real clip with ffmpeg.

Serves `VideoRequest(action="cut" | "caption")` on one input clip named by its sha256. The clip is
read from the tenant's own folder (`<THALLO_BRAND_ROOT>/<tenant>/media-<sha>.mp4`, as ingest
stores it), re-hashed, then:
- cut: `script` may be "START_MS-END_MS"; else the first `duration_ms` (or the whole clip);
  scaled and centre-cropped to the request's size;
- caption: each line of `script` is one caption, shown for an equal share of the clip. Captions
  are drawn to PNG with Pillow and overlaid, so no text ever enters an ffmpeg filter string.

ffmpeg runs from an argv list, never a shell. Operator configuration only: THALLO_BRAND_ROOT,
THALLO_FFMPEG (else ffmpeg on PATH). Note: `ingest_provenance` labels this provider's output
GENERATED (it is neither template_render nor manual); see the WP-11 report.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from thallo.connections.errors import ProviderUnavailable
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, ConnectionMode, MediaKind, MediaProvenance
from thallo.contracts.testing.mocks import MockProvider
from thallo.media.ingest import MAX_VIDEO_BYTES, find_ffmpeg
from thallo.providers.image.http_media import write_output
from thallo.providers.image.template_render import BrandMisconfigured, tenant_paths

NAME = "ffmpeg"
CAPABILITY = Capability.VIDEO
_RANGE = re.compile(r"^\s*(\d{1,8})\s*-\s*(\d{1,8})\s*$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_CAPTIONS = 20


class FfmpegEdit:
    capability = CAPABILITY
    name = NAME

    def __init__(self, clock: Clock, ffmpeg: Path | None, *, tenant_id: str | None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.clock = clock
        self.ffmpeg = ffmpeg
        self.tenant_id = tenant_id
        self.environ: Mapping[str, str] = dict(os.environ if environ is None else environ)

    def problem(self) -> str | None:
        if self.ffmpeg is None:
            return "no ffmpeg on this machine"
        try:
            tenant_paths(self.tenant_id, self.environ)
        except BrandMisconfigured as exc:
            return f"no tenant folder: {exc}"
        return None

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=CAPABILITY, name=NAME, mode=ConnectionMode.LOCAL,
                              limits={"actions": ["cut", "caption"], "inputs": 1},
                              licence_note="local ffmpeg on the client's own clip", sends_externally=False)

    def health(self) -> c.Health:
        issue = self.problem()
        return c.Health(connected=issue is None, checked_at=self.clock.now(), detail=issue)

    def _clip(self, request: c.VideoRequest) -> bytes:
        if len(request.input_sha256) != 1 or not _SHA.match(request.input_sha256[0]):
            raise ProviderUnavailable("cuts and captions take exactly one clip")
        sha = request.input_sha256[0]
        root = tenant_paths(request.tenant_id, self.environ).root
        path = root / f"media-{sha}.mp4"
        try:
            st = os.lstat(path)
        except OSError:
            raise ProviderUnavailable("the clip is not in the tenant's folder") from None
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_VIDEO_BYTES:
            raise ProviderUnavailable("the clip is not a regular file under the size cap")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(MAX_VIDEO_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != sha:
            raise ProviderUnavailable("the stored clip does not match its hash")
        return data

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)  # type: ignore[arg-type]
        assert isinstance(request, c.VideoRequest)
        if request.tenant_id != self.tenant_id:
            raise ProviderUnavailable("this connection belongs to another tenant")
        if request.action not in ("cut", "caption"):
            raise ProviderUnavailable("ffmpeg only cuts and captions")
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"ffmpeg is not configured: {issue}")
        if request.action == "caption" and not _captions(request.script):
            raise ProviderUnavailable("captions need a script, one caption per line")
        self._clip(request)
        return c.Estimate(cost_eur=Decimal(0), seconds=10.0)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.VideoRequest)
        data = self._clip(request)
        w, h = request.width, request.height
        fit = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
        with tempfile.TemporaryDirectory(prefix="thallo-edit-") as tmp:
            src, out = Path(tmp) / "in.mp4", Path(tmp) / "out.mp4"
            src.write_bytes(data)
            argv = [str(self.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
            settings: dict = {"action": request.action, "input": request.input_sha256[0]}
            start, end = 0, request.duration_ms
            if request.action == "cut" and request.script and (m := _RANGE.match(request.script)):
                start, end = int(m.group(1)), int(m.group(2))
                if end <= start:
                    raise ProviderUnavailable("the cut ends before it starts")
            argv += ["-ss", f"{start / 1000:.3f}"]
            if end is not None:
                argv += ["-t", f"{(end - start) / 1000:.3f}"]
            argv += ["-i", str(src)]
            if request.action == "caption":
                caps = _captions(request.script)
                seconds = (end - start) / 1000 if end is not None else _duration(self.ffmpeg, src)
                share = seconds / len(caps)
                chain, last = [f"[0:v]{fit}[b]"], "b"
                for i, text in enumerate(caps):
                    png = Path(tmp) / f"cap{i}.png"
                    png.write_bytes(caption_png(text, w, h))
                    argv += ["-i", str(png)]
                    a, b = i * share, (i + 1) * share
                    chain.append(f"[{last}][{i + 1}:v]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'[o{i}]")
                    last = f"o{i}"
                argv += ["-filter_complex", ";".join(chain), "-map", f"[{last}]", "-map", "0:a:0?"]
                settings["captions"] = caps
            else:
                argv += ["-vf", fit, "-map", "0:v:0", "-map", "0:a:0?"]
            argv += ["-map_metadata", "-1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                     "-movflags", "+faststart", "-fflags", "+bitexact", str(out)]
            try:
                subprocess.run(argv, check=True, capture_output=True, timeout=600)  # noqa: S603 - argv list
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                raise RuntimeError("ffmpeg could not edit the clip") from None
            result = out.read_bytes()
        path, sha = write_output(self.environ, request.tenant_id, result, "mp4")
        media = c.MediaResult(kind=MediaKind.VIDEO, sha256=sha, mime="video/mp4", storage_uri=path.as_uri(),
                              provenance=MediaProvenance.CLIENT_CLIP, width=w, height=h,
                              duration_ms=(end - start) if end is not None else None, disclosure_required=False)
        return c.RunResult(output=media, cost_eur=Decimal(0), seconds=10.0,
                           provenance=c.Provenance(made_by="provider", provider=NAME, mode=ConnectionMode.LOCAL,
                                                   model="ffmpeg", settings=settings, made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(CAPABILITY, self.clock, name=f"mock-{NAME}")


def _captions(script: str | None) -> list[str]:
    lines = [" ".join(x.split())[:120] for x in (script or "").splitlines()]
    return [x for x in lines if x][:MAX_CAPTIONS]


def _duration(ffmpeg: Path | None, src: Path) -> float:
    probe = Path(str(ffmpeg)).with_name("ffprobe")
    try:
        out = subprocess.run([str(probe), "-v", "error", "-show_entries", "format=duration", "-of",  # noqa: S603
                              "default=nw=1:nk=1", str(src)], check=True, capture_output=True, timeout=60)
        return max(float(out.stdout.strip()), 0.5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError):
        raise RuntimeError("could not read the clip's length") from None


def caption_png(text: str, width: int, height: int) -> bytes:
    """A transparent full-frame PNG with the caption in a dark band near the bottom."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    size = max(width // 22, 14)
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - old Pillow
        font = ImageFont.load_default()
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) > width * 0.86 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    pad, lh = size // 2, int(size * 1.3)
    band_h = lh * len(lines) + pad * 2
    top = int(height * 0.80) - band_h
    draw.rectangle([int(width * 0.05), top, int(width * 0.95), top + band_h], fill=(0, 0, 0, 170))
    for i, ln in enumerate(lines):
        tw = draw.textlength(ln, font=font)
        draw.text(((width - tw) / 2, top + pad + i * lh), ln, font=font, fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def create(ctx: ProviderContext) -> FfmpegEdit:
    return FfmpegEdit(ctx.clock, find_ffmpeg(), tenant_id=ctx.tenant_id)


def register(registry: ProviderRegistry) -> None:
    registry.register(CAPABILITY, NAME, ConnectionMode.LOCAL, create, settings_schema=NoSettings)
