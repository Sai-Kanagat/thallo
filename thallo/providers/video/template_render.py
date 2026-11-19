"""Template video: brand HTML frames rendered by headless Chromium, joined into an MP4 by ffmpeg.

The HyperFrames pattern in our own code: every frame is a brand template (default the
`video_frame` family) rendered and design-linted by the image renderer, then ffmpeg shows each
frame for its share of the duration with a short cross-fade. Serves
`VideoRequest(action="template_render")`; each non-empty line of `script` is one frame's caption.

Named `template_render` under the video capability, so `ingest_provenance` labels the file
TEMPLATE_RENDER. The lint report of every frame is in the provenance settings.

Operator configuration, in the environment only: THALLO_BRAND_ROOT (as for the image renderer),
THALLO_CHROMIUM, THALLO_FFMPEG (else ffmpeg on PATH). Without a browser, ffmpeg or a tenant
folder the provider reports itself unavailable and the chain falls through to manual.
"""
from __future__ import annotations

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
from thallo.media.ingest import find_ffmpeg
from thallo.media.render import ChromiumRenderer, Renderer, find_chromium
from thallo.providers.image.http_media import write_output
from thallo.providers.image.template_render import TemplateRender

NAME = "template_render"
CAPABILITY = Capability.VIDEO
DEFAULT_TEMPLATE = "video_frame-03"  # no photo needed
MAX_FRAMES = 12
FADE_S = 0.4
DEFAULT_MS_PER_FRAME = 2500


class TemplateVideo:
    capability = CAPABILITY
    name = NAME

    def __init__(self, clock: Clock, renderer: Renderer | None, ffmpeg: Path | None, *, tenant_id: str | None,
                 mock_tenants=(), environ: Mapping[str, str] | None = None) -> None:  # noqa: ANN001
        self.clock = clock
        self.ffmpeg = ffmpeg
        self.images = TemplateRender(clock, None, renderer, tenant_id=tenant_id, mock_tenants=mock_tenants,
                                     environ=environ)
        self.environ = self.images.environ

    def problem(self) -> str | None:
        if self.ffmpeg is None:
            return "no ffmpeg on this machine"
        h = self.images.health()
        return None if h.connected else (h.detail or "the frame renderer is unavailable")

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=CAPABILITY, name=NAME, mode=ConnectionMode.LOCAL,
                              limits={"actions": ["template_render"], "max_frames": MAX_FRAMES},
                              licence_note="local Chromium and ffmpeg; the brand's own fonts",
                              sends_externally=False)

    def health(self) -> c.Health:
        issue = self.problem()
        return c.Health(connected=issue is None, checked_at=self.clock.now(), detail=issue)

    def _frames(self, request: c.VideoRequest) -> list[str]:
        lines = [" ".join(x.split()) for x in (request.script or "").splitlines()]
        return [x for x in lines if x][:MAX_FRAMES]

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)  # type: ignore[arg-type]
        assert isinstance(request, c.VideoRequest)
        if request.action != "template_render":
            raise ProviderUnavailable("template video only renders templates")
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"template video is not configured: {issue}")
        if not self._frames(request):
            raise ProviderUnavailable("template video needs a script, one caption per line")
        return c.Estimate(cost_eur=Decimal(0), seconds=3.0 * len(self._frames(request)))

    def run(self, request: c.Request) -> c.RunResult:
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.VideoRequest) and self.ffmpeg is not None
        frames = self._frames(request)
        total_ms = request.duration_ms or DEFAULT_MS_PER_FRAME * len(frames)
        each = max(total_ms / 1000 / len(frames), FADE_S * 2)
        pngs, lints = [], []
        for i, caption in enumerate(frames):
            img = c.ImageRequest(tenant_id=request.tenant_id, purpose="template_render",
                                 template_ref=request.template_ref or DEFAULT_TEMPLATE, slots={"caption": caption},
                                 width=request.width, height=request.height,
                                 idempotency_key=f"{request.idempotency_key or 'video'}:frame:{i}")
            res = self.images.run(img)  # lint FAIL raises RenderRejected: no video is made
            pngs.append(Path(res.output.storage_uri.removeprefix("file://")))
            lints.append({"frame": i, "sha256": res.output.sha256, "lint": res.provenance.settings.get("lint")})
        data = self._join(pngs, each, request.width, request.height)
        path, sha = write_output(self.environ, request.tenant_id, data, "mp4")
        duration = round(each * len(frames) * 1000 - FADE_S * 1000 * (len(frames) - 1))
        media = c.MediaResult(kind=MediaKind.VIDEO, sha256=sha, mime="video/mp4", storage_uri=path.as_uri(),
                              provenance=MediaProvenance.TEMPLATE_RENDER, width=request.width,
                              height=request.height, duration_ms=duration, disclosure_required=False)
        settings = {"template": request.template_ref or DEFAULT_TEMPLATE, "frames": lints, "seconds_per_frame": each}
        return c.RunResult(output=media, cost_eur=Decimal(0), seconds=3.0 * len(frames),
                           provenance=c.Provenance(made_by="provider", provider=NAME, mode=ConnectionMode.LOCAL,
                                                   model="chromium+ffmpeg", settings=settings,
                                                   made_at=self.clock.now()))

    def _join(self, pngs: list[Path], each: float, w: int, h: int) -> bytes:
        argv = [str(self.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        for p in pngs:
            argv += ["-loop", "1", "-framerate", "25", "-t", f"{each:.3f}", "-i", str(p)]
        chains = [f"[{i}:v]scale={w}:{h},format=yuv420p,setsar=1[v{i}]" for i in range(len(pngs))]
        last = "v0"
        for i in range(1, len(pngs)):
            offset = i * (each - FADE_S)
            chains.append(f"[{last}][v{i}]xfade=transition=fade:duration={FADE_S}:offset={offset:.3f}[x{i}]")
            last = f"x{i}"
        with tempfile.TemporaryDirectory(prefix="thallo-video-") as tmp:
            out = Path(tmp) / "out.mp4"
            argv += ["-filter_complex", ";".join(chains), "-map", f"[{last}]", "-c:v", "libx264", "-pix_fmt",
                     "yuv420p", "-r", "25", "-movflags", "+faststart", "-fflags", "+bitexact", str(out)]
            try:
                subprocess.run(argv, check=True, capture_output=True, timeout=600)  # noqa: S603 - argv list
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                raise RuntimeError("ffmpeg could not join the frames") from None
            return out.read_bytes()

    def mock(self) -> MockProvider:
        return MockProvider(CAPABILITY, self.clock, name=f"mock-{NAME}")


def create(ctx: ProviderContext) -> TemplateVideo:
    binary = find_chromium()
    renderer = ChromiumRenderer(binary) if binary and binary.is_file() else None
    return TemplateVideo(ctx.clock, renderer, find_ffmpeg(), tenant_id=ctx.tenant_id)


def register(registry: ProviderRegistry) -> None:
    registry.register(CAPABILITY, NAME, ConnectionMode.LOCAL, create, settings_schema=NoSettings)
