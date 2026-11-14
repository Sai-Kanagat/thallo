"""The template_render provider with a fake renderer (no browser): the five calls, the
contract suite, lint failures, the output folder rules, and registration."""
from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from pathlib import Path

import pytest

from thallo.connections import ProviderContext, ProviderRegistry
from thallo.connections.errors import ProviderUnavailable
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, CheckVerdict, ConnectionMode, MediaKind, MediaProvenance
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import _id
from thallo.media.lint import RenderRejected, to_check_results
from thallo.providers.image import template_render as tr
from thallo.providers.image.template_render import (
    RenderOutputError,
    TemplateRender,
    TemplateRenderSettings,
    default_output_dir,
    private_dir,
)

from .conftest import DEMO, FakeRenderer, T, V, req


@pytest.fixture
def out(tmp_path):
    return tmp_path / "renders"  # created by the provider, mode 0o700


@pytest.fixture
def provider(clock, out):
    return TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), FakeRenderer(), **DEMO)


def test_contract_suite_with_fake_renderer(provider, clock):
    reqs = [req("quote-01"), req("quote-02", quote="Hello", attribution="A", _lang="it"),
            req("carousel-01", **{"s1.headline": "a", "s2.headline": "b", "_output": "pdf"})]
    assert_provider_contract(provider, clock, requests=reqs)
    assert provider.describe().mode is ConnectionMode.LOCAL
    assert not provider.describe().sends_externally


def test_result_hash_is_the_bytes_written(provider, out):
    res = provider.run(req("quote-01", quote="Hello", attribution="A"))
    m = res.output
    assert isinstance(m, c.MediaResult)
    assert m.provenance is MediaProvenance.TEMPLATE_RENDER and m.kind is MediaKind.IMAGE
    assert m.mime == "image/png" and (m.width, m.height) == (1080, 1350) and not m.disclosure_required
    path = Path(m.storage_uri.removeprefix("file://"))
    assert path.parent == out and hashlib.sha256(path.read_bytes()).hexdigest() == m.sha256
    assert res.cost_eur == Decimal(0) and res.provenance.made_by == "provider"
    settings = res.provenance.settings
    assert settings["template"] == "quote-01" and settings["preview"] is False and settings["fictional_brand"]
    assert {d["check"] for d in settings["lint"]} >= {"design.contrast", "design.overflow"}
    assert all(d["verdict"] == "pass" for d in settings["lint"])


def test_pdf_output(provider):
    m = provider.run(req("carousel-03", **{"s1.headline": "a", "s2.headline": "b", "_output": "pdf"})).output
    assert m.kind is MediaKind.PDF and m.mime == "application/pdf" and m.storage_uri.endswith(".pdf")


def test_empty_slots_is_a_marked_preview(provider):
    assert provider.run(req("quote-01")).provenance.settings["preview"] is True


def test_static_failure_raises_and_writes_nothing(provider, out, clock):
    with pytest.raises(RenderRejected) as exc:
        provider.run(req("quote-01", quote="x" * 500, attribution="A"))
    assert not out.exists() or not any(out.iterdir())
    assert provider.renderer.calls == []  # never reached the browser
    rows = to_check_results(exc.value.report, tenant_id=T, variant_id=V, clock=clock)
    assert any(r.check_name == "design.slots" and r.verdict is CheckVerdict.FAIL for r in rows)
    assert all(r.verdict is not CheckVerdict.FLAG for r in rows)


def test_layout_failure_raises_and_writes_nothing(clock, out):
    def no_fonts(layout):
        layout["fonts"] = dict.fromkeys(layout["fonts"], False)
        return layout
    p = TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), FakeRenderer(no_fonts), **DEMO)
    with pytest.raises(RenderRejected, match="design.font_loaded"):
        p.run(req("quote-01", quote="Hello", attribution="A"))
    assert p.renderer.calls == ["measure"]
    assert not out.exists() or not any(out.iterdir())


def test_bad_request_is_a_fail_not_a_crash(provider):
    with pytest.raises(RenderRejected, match="design.request"):
        provider.run(req("nope-01"))


def test_unavailable_without_a_browser_or_for_other_purposes(clock, out):
    p = TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), None, **DEMO)
    assert p.health().connected is False
    with pytest.raises(ProviderUnavailable):
        p.estimate(req())
    gen = c.ImageRequest(tenant_id=T, purpose="background", prompt="x", width=10, height=10)
    with pytest.raises(ProviderUnavailable):
        TemplateRender(clock, renderer=FakeRenderer(), **DEMO).estimate(gen)


def test_health_reports_bad_brand(clock, tmp_path):
    p = TemplateRender(clock, TemplateRenderSettings(brand_dir=str(tmp_path)), FakeRenderer(), **DEMO)
    h = p.health()
    assert h.connected is False and "brand" in h.detail


# ---------------------------------------------------------------------- output folder

def test_default_output_dir_is_per_user_not_shared_tmp(monkeypatch, tmp_path):
    import tempfile
    d = default_output_dir({})
    assert not d.is_relative_to(Path(tempfile.gettempdir()))
    assert d.is_relative_to(Path.home())
    assert default_output_dir({"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "thallo" / "renders"


def test_output_dir_created_private(provider, out):
    provider.run(req("quote-01"))
    assert out.stat().st_mode & 0o777 == 0o700


def test_output_dir_symlink_refused(clock, tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    p = TemplateRender(clock, TemplateRenderSettings(output_dir=str(link)), FakeRenderer(), **DEMO)
    with pytest.raises(RenderOutputError, match="symlink"):
        p.run(req("quote-01"))


def test_output_dir_open_to_others_refused(clock, tmp_path):
    d = tmp_path / "open"
    d.mkdir()
    d.chmod(0o755)
    with pytest.raises(RenderOutputError, match="other users"):
        private_dir(d)


def test_output_dir_owned_by_someone_else_refused(tmp_path, monkeypatch):
    d = tmp_path / "x"
    d.mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(d).st_uid + 1)
    with pytest.raises(RenderOutputError, match="owned"):
        private_dir(d)


def test_final_path_symlink_refused(provider, out, tmp_path):
    target = tmp_path / "elsewhere.png"
    target.write_bytes(b"keep")
    first = provider.run(req("quote-01"))  # same request, same bytes, same final name
    final = Path(first.output.storage_uri.removeprefix("file://"))
    final.unlink()
    final.symlink_to(target)
    with pytest.raises(RenderOutputError, match="symlink"):
        provider.run(req("quote-01"))
    assert target.read_bytes() == b"keep"


def test_written_file_is_rehashed(provider, monkeypatch):
    monkeypatch.setattr(tr, "read_sha256", lambda path: "0" * 64)
    with pytest.raises(RenderOutputError, match="does not hash"):
        provider.run(req("quote-01"))
    assert not [p for p in provider.output_dir.iterdir() if p.suffix == ".png"]


def test_no_temp_files_left_behind(provider, out):
    provider.run(req("quote-01"))
    provider.run(req("quote-01"))
    assert [p.suffix for p in out.iterdir()] == [".png"]


# ---------------------------------------------------------------------- registration

def test_registered_with_or_without_a_browser(monkeypatch, tmp_path, clock):
    monkeypatch.setenv("THALLO_CHROMIUM", str(tmp_path / "missing"))
    reg = ProviderRegistry()
    tr.register(reg)
    entry = reg.get(Capability.IMAGE, "template_render")
    assert entry.mode is ConnectionMode.LOCAL
    row = Connection(id=_id(99), created_at=clock.now(), tenant_id=T, capability=Capability.IMAGE,
                     provider="template_render", mode=ConnectionMode.LOCAL)
    p = entry.factory(ProviderContext(T, row, clock))
    assert p.renderer is None and p.health().connected is False
    with pytest.raises(ProviderUnavailable):
        p.estimate(req())


def _registry(clock, mock_tenants=()):
    from thallo.connections import ConnectionRegistry, InMemoryConnectionStore
    return ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                              connections=InMemoryConnectionStore(), mock_tenants=mock_tenants, environ={})


REAL = "0190f3a0-0000-7000-8000-000000000002"


def _image(tenant):
    return c.ImageRequest(tenant_id=tenant, purpose="template_render", template_ref="quote-01", width=1080,
                          height=1350, idempotency_key="k-fallback")


def test_no_browser_falls_back_to_manual(monkeypatch, tmp_path, clock):
    monkeypatch.setenv("THALLO_CHROMIUM", str(tmp_path / "missing"))
    registry = _registry(clock)
    row = registry.add(REAL, Capability.IMAGE, "template_render")  # nothing is written either way
    served = registry.run(REAL, _image(REAL))
    assert served.provider == "manual" and served.mode is ConnectionMode.MANUAL
    assert served.skipped == ((row.id, "unavailable"),)
    assert served.result.output.storage_uri.startswith("manual://pending/")


def test_real_tenant_without_brand_dir_falls_back_to_manual(monkeypatch, tmp_path, clock):
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\nexit 1\n")  # never run: the brand check comes first
    fake.chmod(0o755)
    monkeypatch.setenv("THALLO_CHROMIUM", str(fake))
    registry = _registry(clock)
    row = registry.add(REAL, Capability.IMAGE, "template_render")  # nothing is written either way
    served = registry.run(REAL, _image(REAL))
    assert served.provider == "manual" and served.skipped == ((row.id, "unavailable"),)


def test_fictional_brand_refused_for_a_real_tenant(clock, out):
    from thallo.brand.tokens import DEMO_BRAND_DIR
    for settings in (TemplateRenderSettings(output_dir=str(out)),
                     TemplateRenderSettings(output_dir=str(out), brand_dir=str(DEMO_BRAND_DIR))):
        p = TemplateRender(clock, settings, FakeRenderer(), tenant_id=REAL, mock_tenants={T}, environ={})
        h = p.health()
        assert h.connected is False and "misconfigured" in h.detail
        with pytest.raises(ProviderUnavailable, match="misconfigured"):
            p.run(_image(REAL))
        assert p.renderer.calls == []
    assert not out.exists() or not any(out.iterdir())


def test_fictional_brand_allowed_by_the_mock_rule(clock, out):
    by_list = TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), FakeRenderer(),
                             tenant_id=T, mock_tenants={T}, environ={})
    assert by_list.health().connected and by_list.run(_image(T)).provenance.settings["fictional_brand"]
    by_env = TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), FakeRenderer(),
                            tenant_id=REAL, environ={"THALLO_ALLOW_MOCK": "1"})
    assert by_env.health().connected and by_env.run(_image(REAL)).output.sha256
    not_one = TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), FakeRenderer(),
                             tenant_id=REAL, environ={"THALLO_ALLOW_MOCK": "0"})
    assert not not_one.health().connected


def test_font_fallback_in_the_output_run_fails_and_keeps_nothing(clock, out):
    def fell_back(fonts):
        return dict.fromkeys(fonts, False)
    for patch in (fell_back, lambda fonts: None):
        p = TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)),
                           FakeRenderer(output_fonts=patch), **DEMO)
        with pytest.raises(RenderRejected, match="design.font_loaded"):
            p.run(req("quote-01", quote="Hello", attribution="A"))
        assert p.renderer.calls == ["measure", "png"]
    assert not out.exists() or not any(out.iterdir())


def test_factory_reads_settings(monkeypatch, tmp_path, clock):
    monkeypatch.setenv("THALLO_CHROMIUM", str(tmp_path / "missing"))
    row = Connection(id=_id(99), created_at=clock.now(), tenant_id=T, capability=Capability.IMAGE,
                     provider="template_render", mode=ConnectionMode.LOCAL,
                     settings={"output_dir": str(tmp_path / "o"), "language": "ta"})
    p = tr.create(ProviderContext(T, row, clock))
    assert p.renderer is None and p.output_dir == tmp_path / "o"
    with pytest.raises(ValueError):
        TemplateRenderSettings.model_validate({"api_key": "x"})


# ---------------------------------------------------------------------- PDF determinism (no browser)

def _pdf(stamp: str, uuid: str, ident: str) -> bytes:
    xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><xmp:CreateDate>{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T'
           f'{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}+02:00</xmp:CreateDate>'
           f'<xmpMM:DocumentID>uuid:{uuid}</xmpMM:DocumentID></x:xmpmeta>')
    return (f"%PDF-1.4\n1 0 obj <</Creator (Chromium) /CreationDate (D:{stamp}+02'00') "
            f"/ModDate (D:{stamp}Z)>> endobj\n2 0 obj <</Type /Metadata>> stream\n{xmp}\nendstream\n"
            f"trailer <</ID [<{ident}> <{ident}>]>>\n%%EOF").encode()


def test_pdf_bytes_do_not_depend_on_time():
    from thallo.media.render import deterministic_pdf
    a = _pdf("20261114160419", "1b4e28ba-2fa1-11d2-883f-0016d3cca427", "a1" * 16)
    b = _pdf("20271231235959", "9f0e28ba-2fa1-11d2-883f-0016d3cca999", "ff" * 16)
    na, nb = deterministic_pdf(a), deterministic_pdf(b)
    assert na == nb and len(na) == len(a)
    assert b"(D:20000101000000+00'00')" in na and b"(D:20000101000000Z)" in na
    assert b"2000-01-01T00:00:00+00:00" in na and b"2026" not in na
    assert deterministic_pdf(na) == na


def test_pdf_with_unreadable_metadata_is_refused():
    from thallo.media.render import PdfNotDeterministic, deterministic_pdf
    with pytest.raises(PdfNotDeterministic):
        deterministic_pdf(b"%PDF-1.4\n1 0 obj <</Type /Metadata /Filter /FlateDecode>> stream\nx\x9c\nendstream")
