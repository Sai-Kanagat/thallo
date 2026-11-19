"""Real ffmpeg and Chromium runs. Skipped, with the reason, where the binary is missing."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from thallo.contracts import connection as c
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.media.ingest import clean_video, find_ffmpeg, ingest_result, ingest_upload
from thallo.media.render import ChromiumRenderer, find_chromium
from thallo.providers.video.ffmpeg_edit import FfmpegEdit
from thallo.providers.video.template_render import TemplateVideo

from .conftest import T

FFMPEG = find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg on this machine (set THALLO_FFMPEG); skipped")


def make_clip(tmp: Path) -> bytes:
    out = tmp / "src.mp4"
    subprocess.run([str(FFMPEG), "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",  # noqa: S603
                    "testsrc=size=320x240:rate=10:duration=2", "-f", "lavfi", "-i", "sine=duration=2",
                    "-metadata", "location=+45.0+009.0/", "-metadata", "title=secret-title",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)], check=True)
    return out.read_bytes()


@needs_ffmpeg
def test_video_upload_drops_location_and_keeps_voice_flag(rows, blobs, registry, clock, tmp_path):
    raw = make_clip(tmp_path)
    assert b"secret-title" in raw
    clean, mime = clean_video(raw, "video/mp4")
    assert mime == "video/mp4" and b"secret-title" not in clean and b"+45.0" not in clean
    got = ingest_upload(rows, blobs, registry, tenant_id=T, data=raw, mime="video/mp4", clock=clock)
    assert got.media.width == 320 and got.media.height == 240 and got.media.has_voice_track


@needs_ffmpeg
def test_ffmpeg_cut_and_caption(rows, blobs, registry, clock, env, root, tmp_path):
    up = ingest_upload(rows, blobs, registry, tenant_id=T, data=make_clip(tmp_path), mime="video/mp4", clock=clock)
    p = FfmpegEdit(clock, FFMPEG, tenant_id=T, environ=env)
    sha = up.media.sha256
    cut = c.VideoRequest(tenant_id=T, action="cut", input_sha256=(sha,), script="0-1000", width=180, height=320,
                         idempotency_key="k-cut")
    cap = c.VideoRequest(tenant_id=T, action="caption", input_sha256=(sha,), script="First line\nSecond line",
                         width=180, height=320, idempotency_key="k-cap")
    assert_provider_contract(p, clock, [cut, cap])
    res = p.run(cap)
    data = Path(res.output.storage_uri.removeprefix("file://")).read_bytes()
    assert hashlib.sha256(data).hexdigest() == res.output.sha256 and res.provenance.settings["captions"]
    with pytest.raises(Exception, match="not in the tenant"):
        p.estimate(cut.model_copy(update={"input_sha256": ("0" * 64,)}))


@needs_ffmpeg
def test_template_video_renders_frames_to_mp4(rows, blobs, clock, root, env):
    binary = find_chromium()
    if binary is None:
        pytest.skip("no headless Chromium on this machine (set THALLO_CHROMIUM); skipped")
    p = TemplateVideo(clock, ChromiumRenderer(binary, timeout_s=300), FFMPEG, tenant_id=T, mock_tenants={T},
                      environ=env)
    req = c.VideoRequest(tenant_id=T, action="template_render", script="Fresh bread at seven\nOpen until noon",
                         width=1080, height=1920, duration_ms=3000, idempotency_key="k-tv")
    assert p.health().connected, p.health().detail
    res = p.run(req)
    assert res.output.mime == "video/mp4" and len(res.provenance.settings["frames"]) == 2
    from thallo.connections.registry import Served
    from thallo.contracts.enums import ConnectionMode, MediaProvenance

    served = Served("0190f3a0-0000-7000-8000-0000000000e2", "template_render", ConnectionMode.LOCAL, res)
    got = ingest_result(rows, blobs, served, req, clock=clock)
    assert got.media.provenance is MediaProvenance.TEMPLATE_RENDER and not got.file.flagged
    assert os.path.getsize(res.output.storage_uri.removeprefix("file://")) > 1000
