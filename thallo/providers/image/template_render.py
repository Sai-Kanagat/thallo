"""Local template render: brand tokens plus an HTML template, rendered by headless Chromium.

Serves `ImageRequest(purpose="template_render")`. The result's sha256 is of the PNG or PDF bytes
actually written, so the gate hashes real bytes. Every render passes the design lint first; a
failing render raises `RenderRejected` carrying the FAIL report, and no file is kept.

Settings: `brand_dir` (required for a real tenant; without it only a mock tenant gets the
fictional demo brand), `templates_dir`, `output_dir` (default: the per-user
`~/.cache/thallo/renders`, mode 0o700), `photo_dirs`, `language` (used when the request has no
`_lang`), `chromium` (path to the browser).

A brand marked `fictional` is refused unless the tenant is a mock tenant, by the registry's own
rule (`mock_tenants`, or THALLO_ALLOW_MOCK=1). The factory only sees the environment half of it;
a caller holding the `ConnectionRegistry` passes `mock_tenants` to the constructor.

The provider is always registered. On a machine with no Chromium, `health()` and `estimate()`
report it unavailable, so the registry skips it and the chain falls through to manual.
"""
from __future__ import annotations

import hashlib
import logging
import os
import stat
import tempfile
from collections.abc import Collection, Mapping
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from thallo.brand.templates import TemplateBook, default_templates, load_templates
from thallo.brand.tokens import DEMO_BRAND_DIR, Brand, TokenError, load_brand
from thallo.connections.errors import ProviderUnavailable
from thallo.connections.registry import ALLOW_MOCK_ENV, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, ConnectionMode, Language, MediaKind, MediaProvenance
from thallo.contracts.testing.mocks import MockProvider
from thallo.media.lint import (
    RenderRejected,
    lint_layout,
    lint_output_fonts,
    lint_static,
    parse_layout,
    request_failure,
)
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
                 renderer: Renderer | None = None, *, tenant_id: str | None = None,
                 mock_tenants: Collection[str] = (), environ: Mapping[str, str] | None = None) -> None:
        """`tenant_id` is the connection's tenant (what `health()` judges); `mock_tenants` and
        `environ` are the registry's mock rule, the same arguments `ConnectionRegistry` takes."""
        self.clock = clock
        self.settings = settings or TemplateRenderSettings()
        self.renderer = renderer
        self.tenant_id = tenant_id
        self.mock_tenants = frozenset(mock_tenants)
        self._allow_mock_everywhere = (os.environ if environ is None else environ).get(ALLOW_MOCK_ENV) == "1"
        s = self.settings
        self.output_dir = Path(s.output_dir) if s.output_dir else default_output_dir()
        self.brand_dir = Path(s.brand_dir) if s.brand_dir else None

    def mock_allowed(self, tenant_id: str | None) -> bool:
        """`ConnectionRegistry.mock_allowed`, applied to the brand."""
        return self._allow_mock_everywhere or (tenant_id is not None and tenant_id in self.mock_tenants)

    # ------------------------------------------------------------------ loading

    def _brand(self, tenant_id: str | None) -> Brand:
        """The tenant's brand. A real tenant must name its `brand_dir`, and may never render a
        brand marked fictional: a real post must not silently carry the demo brand."""
        mock = self.mock_allowed(tenant_id)
        if self.brand_dir is None:
            if not mock:
                raise BrandMisconfigured("no brand_dir set for this tenant; the demo brand is only for mock tenants")
            return load_brand(DEMO_BRAND_DIR)
        brand = load_brand(self.brand_dir)
        if brand.tokens.fictional and not mock:
            raise BrandMisconfigured(f"brand {brand.tokens.brand!r} is marked fictional; only a mock tenant may use it")
        return brand

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
            self._brand(self.tenant_id)
            self._book()
        except BrandMisconfigured as exc:
            return c.Health(connected=False, checked_at=self.clock.now(), detail=f"misconfigured: {exc}")
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
        try:
            self._brand(request.tenant_id)
        except BrandMisconfigured as exc:
            raise ProviderUnavailable(f"misconfigured: {exc}") from None
        except TokenError:
            pass  # a broken brand file is reported by run() as a design.request FAIL
        return c.Estimate(cost_eur=Decimal(0), seconds=ESTIMATE_SECONDS)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)  # type: ignore[arg-type]
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.ImageRequest) and self.renderer is not None
        try:
            brand = self._brand(request.tenant_id)  # BrandMisconfigured is not caught: it is no lint verdict
            book = self._book()
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
        rendered = render_bytes(self.renderer, job, document)
        report = report.merged(lint_output_fonts(job, rendered.fonts))
        if report.failed:
            raise RenderRejected(report)  # the bytes are dropped; nothing was written
        data = rendered.data
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


class BrandMisconfigured(ProviderUnavailable):
    """No brand for a real tenant, or a fictional brand for one. Raised from `run()`, it stops
    the chain like any other error from inside a provider's run."""


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
    if renderer is None:
        log.info("template_render: no headless Chromium found (set THALLO_CHROMIUM); reporting unavailable")
    return TemplateRender(ctx.clock, settings, renderer, tenant_id=ctx.tenant_id)


def register(registry: ProviderRegistry) -> None:
    registry.register(CAPABILITY, NAME, ConnectionMode.LOCAL, create, settings_schema=TemplateRenderSettings)
