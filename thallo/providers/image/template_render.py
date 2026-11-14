"""Local template render: brand tokens plus an HTML template, rendered by headless Chromium.

Serves `ImageRequest(purpose="template_render")`. The result's sha256 is of the PNG or PDF bytes
actually written, so the gate hashes real bytes. Every render passes the design lint first; a
failing render raises `RenderRejected` carrying the FAIL report, and no file is kept.

Connection settings are tenant-writable (the connections screen exposes them), so they name no
binary, no path and no host. The one setting is `language` (used when the request has no `_lang`).

Operator configuration, in the environment only:
    THALLO_CHROMIUM     the browser binary; without it `find_chromium()` looks on this machine
    THALLO_BRAND_ROOT   an absolute directory the operator owns, one folder per tenant id:
        <root>/<tenant_id>/brand/       the tenant's brand (tokens, script tokens, logo, fonts)
        <root>/<tenant_id>/templates/   optional; the repository templates when absent
        <root>/<tenant_id>/photos/      optional; photos a request may name
        <root>/<tenant_id>/renders/     the output, created with mode 0o700
Each of those is resolved (symlinks followed) and must sit inside that tenant's own folder, and
the tenant folder itself must be a plain directory directly under the root. A tenant id that is
not a plain folder name is refused. With THALLO_BRAND_ROOT unset or unusable, the provider is
unavailable for every tenant, so the chain falls through to manual.

A connection row that still carries a removed setting (`chromium`, `brand_dir`, `templates_dir`,
`output_dir`, `photo_dirs`) is never used: the factory logs a warning and the provider reports
itself unavailable.

A brand marked `fictional` is refused unless the tenant is a mock tenant, by the registry's own
rule (`mock_tenants`, or THALLO_ALLOW_MOCK=1). A mock tenant with no brand folder gets the
fictional demo brand. The `fictional` flag is read from the brand's own tokens file; that file is
operator data, because every brand folder now sits under the operator's root and no tenant
setting can point elsewhere. The factory only sees the environment half of the mock rule; a
caller holding the `ConnectionRegistry` passes `mock_tenants` to the constructor.

The provider is always registered. On a machine with no Chromium, `health()` and `estimate()`
report it unavailable, so the registry skips it and the chain falls through to manual.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from thallo.brand.templates import TemplateBook, default_templates, load_templates
from thallo.brand.tokens import DEMO_BRAND_DIR, Brand, TokenError, load_brand
from thallo.connections.errors import InvalidConnection, ProviderUnavailable
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
BRAND_ROOT_ENV = "THALLO_BRAND_ROOT"
CHROMIUM_ENV = "THALLO_CHROMIUM"
REMOVED_SETTINGS = frozenset({"chromium", "brand_dir", "templates_dir", "output_dir", "photo_dirs"})
TENANT_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")  # one plain folder name, never . or ..


class TemplateRenderSettings(BaseModel):
    """Tenant-writable: nothing here may name a binary, a path or a host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: Language = Language.EN


@dataclass(frozen=True)
class TenantPaths:
    """One tenant's folders under the operator's root, each resolved and inside `root`."""

    root: Path
    brand: Path | None
    templates: Path | None
    photos: Path | None
    output: Path


def tenant_paths(tenant_id: str | None, environ: Mapping[str, str]) -> TenantPaths:
    """The tenant's folders under THALLO_BRAND_ROOT. Raises `BrandMisconfigured` (so the
    provider is unavailable) when the root is unset or unusable, the tenant id is not a plain
    folder name, the tenant folder is not a plain directory directly under the root, or a
    sub-folder resolves outside the tenant's folder."""
    raw = environ.get(BRAND_ROOT_ENV, "")
    if not raw:
        raise BrandMisconfigured(f"{BRAND_ROOT_ENV} is not set; the operator gives each tenant a brand folder")
    base = Path(raw)
    if not base.is_absolute() or not base.is_dir():
        raise BrandMisconfigured(f"{BRAND_ROOT_ENV} must be an absolute path to an existing directory")
    base = base.resolve()
    if tenant_id is None or not TENANT_SEGMENT.match(tenant_id):
        raise BrandMisconfigured("the tenant id is not a plain folder name")
    root = base / tenant_id
    if os.path.lexists(root) and (root.is_symlink() or not root.is_dir() or root.resolve() != root):
        raise BrandMisconfigured("the tenant folder must be a plain directory directly under the brand root")

    def inside(name: str) -> Path | None:
        p = root / name
        if not os.path.lexists(p):
            return None
        real = p.resolve()
        if not real.is_relative_to(root) or not real.is_dir():
            raise BrandMisconfigured(f"the tenant's {name} folder resolves outside the tenant folder")
        return real

    return TenantPaths(root=root, brand=inside("brand"), templates=inside("templates"), photos=inside("photos"),
                       output=root / "renders")


class TemplateRender:
    capability = CAPABILITY
    name = NAME

    def __init__(self, clock: Clock, settings: TemplateRenderSettings | None = None,
                 renderer: Renderer | None = None, *, tenant_id: str | None = None,
                 mock_tenants: Collection[str] = (), environ: Mapping[str, str] | None = None,
                 problem: str | None = None) -> None:
        """`tenant_id` is the connection's tenant: what `health()` judges, and the only tenant
        this provider serves. `mock_tenants` and `environ` are the registry's mock rule, the same
        arguments `ConnectionRegistry` takes; `environ` also carries THALLO_BRAND_ROOT.
        `problem`, when set, makes the provider unavailable (settings the factory refused)."""
        self.clock = clock
        self.settings = settings or TemplateRenderSettings()
        self.renderer = renderer
        self.tenant_id = tenant_id
        self.mock_tenants = frozenset(mock_tenants)
        self.environ: Mapping[str, str] = dict(os.environ if environ is None else environ)
        self._allow_mock_everywhere = self.environ.get(ALLOW_MOCK_ENV) == "1"
        self.problem = problem

    def mock_allowed(self, tenant_id: str | None) -> bool:
        """`ConnectionRegistry.mock_allowed`, applied to the brand."""
        return self._allow_mock_everywhere or (tenant_id is not None and tenant_id in self.mock_tenants)

    def paths(self, tenant_id: str | None = None) -> TenantPaths:
        """The folders for `tenant_id` (default: the connection's tenant). A request for another
        tenant than the connection's is refused outright."""
        if self.problem is not None:
            raise BrandMisconfigured(self.problem)
        if tenant_id is None:
            tenant_id = self.tenant_id
        elif self.tenant_id is not None and tenant_id != self.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")
        return tenant_paths(tenant_id, self.environ)

    @property
    def output_dir(self) -> Path:
        return self.paths().output

    # ------------------------------------------------------------------ loading

    def _brand(self, paths: TenantPaths, tenant_id: str | None) -> Brand:
        """The tenant's brand, from its folder under the operator's root. A real tenant must have
        one, and may never render a brand marked fictional: a real post must not silently carry
        the demo brand."""
        mock = self.mock_allowed(tenant_id)
        if paths.brand is None:
            if not mock:
                raise BrandMisconfigured("this tenant has no brand folder; the demo brand is only for mock tenants")
            return load_brand(DEMO_BRAND_DIR)
        brand = load_brand(paths.brand)
        if brand.tokens.fictional and not mock:
            raise BrandMisconfigured(f"brand {brand.tokens.brand!r} is marked fictional; only a mock tenant may use it")
        return brand

    def _book(self, paths: TenantPaths) -> TemplateBook:
        return load_templates(paths.templates) if paths.templates is not None else default_templates()

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
            paths = self.paths()
            self._brand(paths, self.tenant_id)
            self._book(paths)
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
            self._brand(self.paths(request.tenant_id), request.tenant_id)
        except BrandMisconfigured as exc:
            raise ProviderUnavailable(f"misconfigured: {exc}") from None
        except TokenError:
            pass  # a broken brand file is reported by run() as a design.request FAIL
        return c.Estimate(cost_eur=Decimal(0), seconds=ESTIMATE_SECONDS)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)  # type: ignore[arg-type]
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.ImageRequest) and self.renderer is not None
        paths = self.paths(request.tenant_id)  # BrandMisconfigured is not caught: it is no lint verdict
        try:
            brand = self._brand(paths, request.tenant_id)
            book = self._book(paths)
            photo_roots = ([paths.photos] if paths.photos is not None else []) + [brand.root]
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
        path = self._write(paths.output, sha, job.output, data)
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

    def _write(self, output: Path, sha: str, ext: str, data: bytes) -> Path:
        """Content-addressed write into a private folder. The temp file is created exclusively
        inside that folder, the final name must not be a symlink, and the final file is read back
        and re-hashed: the gate trusts the sha256 returned in MediaResult."""
        folder = private_dir(output)
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
    """No usable brand root, no brand for a real tenant, or a fictional brand for one. Raised
    from `run()`, it stops the chain like any other error from inside a provider's run."""


class RenderOutputError(RuntimeError):
    """The output folder or file is not safe to use. Nothing is returned for the gate to hash."""


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
    """The browser comes from operator configuration only (THALLO_CHROMIUM or `find_chromium()`),
    never from the connection row. A row whose settings this schema does not accept (above all
    a removed path or binary setting) is never used: the provider reports itself unavailable."""
    raw = dict(ctx.connection.settings or {})
    problem = None
    try:
        settings = TemplateRenderSettings.model_validate(raw)
    except ValidationError:
        settings = TemplateRenderSettings()
        removed = sorted(REMOVED_SETTINGS & raw.keys())
        problem = (f"the connection settings carry keys that are no longer accepted: {removed}" if removed
                   else "the connection settings do not match this provider's schema")
        log.warning("template_render: connection %s refused: %s", ctx.connection.id, problem)
    binary = find_chromium()
    renderer = ChromiumRenderer(binary) if binary and binary.is_file() else None
    if renderer is None:
        log.info("template_render: no headless Chromium found (set %s); reporting unavailable", CHROMIUM_ENV)
    return TemplateRender(ctx.clock, settings, renderer, tenant_id=ctx.tenant_id, problem=problem)


def register(registry: ProviderRegistry) -> None:
    registry.register(CAPABILITY, NAME, ConnectionMode.LOCAL, create, settings_schema=TemplateRenderSettings)
