"""Local template render: brand tokens plus an HTML template, rendered by headless Chromium.

Serves `ImageRequest(purpose="template_render")`. The result's sha256 is of the PNG or PDF bytes
actually written, so the gate hashes real bytes. Every render passes the design lint first; a
failing render raises `RenderRejected` carrying the FAIL report, and no file is kept.

Settings (all optional): `brand_dir` (default: the fictional demo brand), `templates_dir`,
`output_dir` (default: the per-user `~/.cache/thallo/renders`, mode 0o700), `photo_dirs`,
`language` (used when the request has no `_lang`), `chromium` (path to the browser).

The provider is registered only on a machine where a Chromium binary is found (see
`find_chromium`), because the shared contract test runs every registered provider. Where there
is none, a connection naming it fails loudly as an unknown provider.
"""
from __future__ import annotations

import hashlib
import logging
import os
import stat
import tempfile
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from thallo.brand.templates import TemplateBook, default_templates, load_templates
from thallo.brand.tokens import DEMO_BRAND_DIR, Brand, TokenError, load_brand
from thallo.connections.errors import ProviderUnavailable
from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, ConnectionMode, Language, MediaKind, MediaProvenance
from thallo.contracts.testing.mocks import MockProvider
from thallo.media.lint import RenderRejected, lint_layout, lint_static, parse_layout, request_failure
from thallo.media.render import (
    ChromiumRenderer,
    PlanError,
    Renderer,
    compose,
    find_chromium,
    plan_job,
    render_bytes,
)

log = logging.getLogger(__name__)

NAME = "template_render"
CAPABILITY = Capability.IMAGE
ESTIMATE_SECONDS = 2.0


class TemplateRenderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brand_dir: str | None = None
    templates_dir: str | None = None
    output_dir: str | None = None
    photo_dirs: tuple[str, ...] = ()
    language: Language = Language.EN
    chromium: str | None = None


class TemplateRender:
    capability = CAPABILITY
    name = NAME

    def __init__(self, clock: Clock, settings: TemplateRenderSettings | None = None,
                 renderer: Renderer | None = None) -> None:
        self.clock = clock
        self.settings = settings or TemplateRenderSettings()
        self.renderer = renderer
        s = self.settings
        self.output_dir = Path(s.output_dir) if s.output_dir else default_output_dir()
        self.brand_dir = Path(s.brand_dir) if s.brand_dir else DEMO_BRAND_DIR

    # ------------------------------------------------------------------ loading

    def _brand(self) -> Brand:
        return load_brand(self.brand_dir)

    def _book(self) -> TemplateBook:
        return load_templates(self.settings.templates_dir) if self.settings.templates_dir else default_templates()

    # ------------------------------------------------------------------ the five calls

    def describe(self) -> c.ProviderInfo:
        version = self.renderer.version() if self.renderer else "none"
        return c.ProviderInfo(
            capability=CAPABILITY, name=NAME, mode=ConnectionMode.LOCAL, models=(version,),
            limits={"outputs": ["png", "pdf"], "purposes": ["template_render"], "max_slides": 10},
            licence_note="local headless Chromium; fonts are the brand's own files or installed fonts, "
                         "whose licence the brand must hold",
            sends_externally=False)

    def health(self) -> c.Health:
        if self.renderer is None:
            return c.Health(connected=False, checked_at=self.clock.now(), detail="no headless Chromium found")
        try:
            self._brand()
            self._book()
        except TokenError as exc:
            return c.Health(connected=False, checked_at=self.clock.now(), detail=f"brand or templates: {exc}")
        return c.Health(connected=True, checked_at=self.clock.now(), detail=self.renderer.version())

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)  # type: ignore[arg-type]
        assert isinstance(request, c.ImageRequest)
        if request.purpose != "template_render":
            raise ProviderUnavailable("template_render only renders templates")
        if self.renderer is None:
            raise ProviderUnavailable("no headless Chromium on this machine")
        return c.Estimate(cost_eur=Decimal(0), seconds=ESTIMATE_SECONDS)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)  # type: ignore[arg-type]
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.ImageRequest) and self.renderer is not None
        try:
            brand, book = self._brand(), self._book()
            photo_roots = [Path(p) for p in self.settings.photo_dirs] + [brand.root]
            job = plan_job(request, brand=brand, book=book, default_language=self.settings.language,
                           photo_roots=photo_roots)
        except (PlanError, TokenError) as exc:
            raise RenderRejected(request_failure(str(exc))) from None
        report = lint_static(job)
        if report.failed:
            raise RenderRejected(report)
        document = compose([job])
        layout = parse_layout(self.renderer.measure(document, job.width, job.height))
        report = report.merged(lint_layout(job, layout))
        if report.failed:
            raise RenderRejected(report)
        data = render_bytes(self.renderer, job, document)
        sha = hashlib.sha256(data).hexdigest()
        path = self._write(sha, job.output, data)
        pdf = job.output == "pdf"
        media = c.MediaResult(kind=MediaKind.PDF if pdf else MediaKind.IMAGE, sha256=sha,
                              mime="application/pdf" if pdf else "image/png", storage_uri=path.as_uri(),
                              provenance=MediaProvenance.TEMPLATE_RENDER, width=job.width, height=job.height,
                              disclosure_required=False)
        settings = {"template": job.template.ref, "brand": brand.tokens.brand,
                    "fictional_brand": brand.tokens.fictional, "language": job.language.value,
                    "channel": job.channel.value if job.channel else None,
                    "output": job.output, "slides": [s.number for s in job.slides], "preview": job.preview,
                    "bytes": len(data), "lint": report.as_dicts()}
        return c.RunResult(output=media, cost_eur=Decimal(0), seconds=ESTIMATE_SECONDS,
                           provenance=c.Provenance(made_by="provider", provider=NAME, mode=ConnectionMode.LOCAL,
                                                   model=self.renderer.version(), settings=settings,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(CAPABILITY, self.clock, name=f"mock-{NAME}")

    # ------------------------------------------------------------------ internals

    def _write(self, sha: str, ext: str, data: bytes) -> Path:
        """Content-addressed write into a private folder. The temp file is created exclusively
        inside that folder, the final name must not be a symlink, and the final file is read back
        and re-hashed: the gate trusts the sha256 returned in MediaResult."""
        folder = private_dir(self.output_dir)
        final = folder / f"{sha}.{ext}"
        if final.is_symlink():
            raise RenderOutputError(f"refusing to write over a symlink: {final.name}")
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".render-", suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            try:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        try:
            if final.is_symlink():
                raise RenderOutputError(f"refusing to write over a symlink: {final.name}")
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if read_sha256(final) != sha:
            final.unlink(missing_ok=True)
            raise RenderOutputError(f"{final.name} does not hash to the bytes rendered; removed")
        return final


class RenderOutputError(RuntimeError):
    """The output folder or file is not safe to use. Nothing is returned for the gate to hash."""


def default_output_dir(environ: dict[str, str] | None = None) -> Path:
    """A per-user private folder, never a predictable path under the shared temp dir."""
    env = os.environ if environ is None else environ
    base = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else Path.home() / ".cache"
    return base / "thallo" / "renders"


def private_dir(path: Path) -> Path:
    """Create `path` with mode 0o700 if missing; refuse a symlink, a folder owned by someone
    else, or one that group or others can open."""
    path = Path(path)
    if path.is_symlink():
        raise RenderOutputError(f"output folder {path} is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RenderOutputError(f"output folder {path} is not a plain directory")
    if st.st_uid != os.getuid():
        raise RenderOutputError(f"output folder {path} is not owned by the current user")
    if st.st_mode & 0o077:
        raise RenderOutputError(f"output folder {path} is open to other users (mode {st.st_mode & 0o777:o})")
    return path


def read_sha256(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    h = hashlib.sha256()
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create(ctx: ProviderContext) -> TemplateRender:
    settings = TemplateRenderSettings.model_validate(ctx.connection.settings or {})
    binary = Path(settings.chromium) if settings.chromium else find_chromium()
    renderer = ChromiumRenderer(binary) if binary and binary.is_file() else None
    return TemplateRender(ctx.clock, settings, renderer)


def register(registry: ProviderRegistry) -> None:
    if find_chromium() is None:
        log.info("template_render not registered: no headless Chromium found (set THALLO_CHROMIUM)")
        return
    registry.register(CAPABILITY, NAME, ConnectionMode.LOCAL, create, settings_schema=TemplateRenderSettings)
