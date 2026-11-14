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

from .conftest import FakeRenderer, T, V, req


@pytest.fixture
def out(tmp_path):
    return tmp_path / "renders"  # created by the provider, mode 0o700


@pytest.fixture
def provider(clock, out):
    return TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), FakeRenderer())


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
    p = TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), FakeRenderer(no_fonts))
    with pytest.raises(RenderRejected, match="design.font_loaded"):
        p.run(req("quote-01", quote="Hello", attribution="A"))
    assert p.renderer.calls == ["measure"]
    assert not out.exists() or not any(out.iterdir())


def test_bad_request_is_a_fail_not_a_crash(provider):
    with pytest.raises(RenderRejected, match="design.request"):
        provider.run(req("nope-01"))


def test_unavailable_without_a_browser_or_for_other_purposes(clock, out):
    p = TemplateRender(clock, TemplateRenderSettings(output_dir=str(out)), None)
    assert p.health().connected is False
    with pytest.raises(ProviderUnavailable):
        p.estimate(req())
    gen = c.ImageRequest(tenant_id=T, purpose="background", prompt="x", width=10, height=10)
    with pytest.raises(ProviderUnavailable):
        TemplateRender(clock, renderer=FakeRenderer()).estimate(gen)


def test_health_reports_bad_brand(clock, tmp_path):
    p = TemplateRender(clock, TemplateRenderSettings(brand_dir=str(tmp_path)), FakeRenderer())
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
    p = TemplateRender(clock, TemplateRenderSettings(output_dir=str(link)), FakeRenderer())
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

def test_registered_only_where_a_browser_exists(monkeypatch, tmp_path):
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("THALLO_CHROMIUM", str(fake))
    reg = ProviderRegistry()
    tr.register(reg)
    assert reg.get(Capability.IMAGE, "template_render").mode is ConnectionMode.LOCAL
    monkeypatch.setenv("THALLO_CHROMIUM", str(tmp_path / "missing"))
    reg = ProviderRegistry()
    tr.register(reg)
    assert "template_render" not in reg.names(Capability.IMAGE)


def test_factory_reads_settings(monkeypatch, tmp_path, clock):
    monkeypatch.setenv("THALLO_CHROMIUM", str(tmp_path / "missing"))
    row = Connection(id=_id(99), created_at=clock.now(), tenant_id=T, capability=Capability.IMAGE,
                     provider="template_render", mode=ConnectionMode.LOCAL,
                     settings={"output_dir": str(tmp_path / "o"), "language": "ta"})
    p = tr.create(ProviderContext(T, row, clock))
    assert p.renderer is None and p.output_dir == tmp_path / "o"
    with pytest.raises(ValueError):
        TemplateRenderSettings.model_validate({"api_key": "x"})
