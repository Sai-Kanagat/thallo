"""Security fixes 1 and 2 (HANDOFF, 14 Nov): connection settings are tenant-writable, so the
renderer takes no binary and no path from them. The browser comes from THALLO_CHROMIUM or
`find_chromium()`; every folder comes from THALLO_BRAND_ROOT/<tenant_id>, resolved and kept
inside that tenant's folder."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from thallo.brand.tokens import DEMO_BRAND_DIR
from thallo.connections import ConnectionRegistry, InMemoryConnectionStore, ProviderContext, ProviderRegistry
from thallo.connections.errors import InvalidConnection, ProviderUnavailable
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.models import Connection
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.contracts.testing.samples import _id
from thallo.media.lint import RenderRejected
from thallo.providers.image import template_render as tr
from thallo.providers.image.template_render import (
    BrandMisconfigured,
    TemplateRender,
    TemplateRenderSettings,
    tenant_paths,
)

from .conftest import FakeRenderer, T, demo, req

A = "0190f3a0-0000-7000-8000-00000000000a"  # two real tenants
B = "0190f3a0-0000-7000-8000-00000000000b"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def real_brand(root: Path, tenant: str, name: str) -> Path:
    """A non-fictional brand for `tenant`, made from the demo's files."""
    d = root / tenant / "brand"
    shutil.copytree(DEMO_BRAND_DIR, d)
    tokens = json.loads((d / "demo.brand.json").read_text())
    tokens.update(brand=name, name=name, fictional=False)
    (d / "brand.json").write_text(json.dumps(tokens))
    (d / "demo.brand.json").unlink()
    return d


def real(root: Path, tenant: str, renderer=None) -> TemplateRender:
    return TemplateRender(_clock(), TemplateRenderSettings(), renderer or FakeRenderer(), tenant_id=tenant,
                          environ={"THALLO_BRAND_ROOT": str(root)})


def _clock():
    from thallo.contracts.clock import FAKE_EPOCH, FakeClock
    return FakeClock(FAKE_EPOCH)


def image(tenant: str, ref: str = "quote-01", **slots: str) -> c.ImageRequest:
    return c.ImageRequest(tenant_id=tenant, purpose="template_render", template_ref=ref, width=1080, height=1350,
                          slots=slots, idempotency_key=f"k-{tenant}-{ref}")


def _registry(clock, environ=None, mock_tenants=()):
    return ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                              connections=InMemoryConnectionStore(), mock_tenants=mock_tenants,
                              environ=environ or {})


# ---------------------------------------------------------------------- fix 1: the browser binary

@pytest.fixture
def evil(tmp_path):
    """A 'browser' that would leave a marker if anything ever executed it."""
    marker = tmp_path / "executed"
    script = tmp_path / "evil-chromium"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    script.chmod(0o755)
    return script, marker


def test_settings_row_naming_a_chromium_path_is_never_executed(evil, tmp_path, monkeypatch, clock, brand_root,
                                                               caplog):
    """A row written before the fix (or straight into the store) still carries `chromium`. With
    no browser in the operator's environment the old factory ran exactly that binary."""
    script, marker = evil
    monkeypatch.setenv("THALLO_CHROMIUM", str(tmp_path / "missing"))  # the operator has no browser
    monkeypatch.setenv("THALLO_BRAND_ROOT", str(brand_root))
    registry = _registry(clock, mock_tenants={T})
    registry.ensure_manual(T, Capability.IMAGE)
    row = Connection(id=_id(77), created_at=clock.now(), tenant_id=T, capability=Capability.IMAGE,
                     provider="template_render", mode=ConnectionMode.LOCAL, priority=0,
                     settings={"chromium": str(script)})
    registry.connections.put(row)  # bypasses add(): the store is tenant-writable through WP-09
    with caplog.at_level(logging.WARNING, logger=tr.__name__):
        provider = registry.build(row)
        assert provider.renderer is None or Path(provider.renderer.binary) != script
        assert provider.health().connected is False
        served = registry.run(T, image(T))
    assert served.provider == "manual" and served.skipped == ((row.id, "unavailable"),)
    assert not marker.exists()
    assert any("chromium" in r.getMessage() for r in caplog.records)


def test_a_removed_setting_makes_the_provider_unavailable_even_with_a_browser(evil, monkeypatch, clock, brand_root):
    """The operator's browser exists, so the row is refused for its settings, not for want of a browser."""
    script, marker = evil
    good = brand_root.parent / "operator-chromium"
    good.write_text("#!/bin/sh\nexit 0\n")
    good.chmod(0o755)
    monkeypatch.setenv("THALLO_CHROMIUM", str(good))
    monkeypatch.setenv("THALLO_BRAND_ROOT", str(brand_root))
    for key, value in (("chromium", str(script)), ("brand_dir", str(DEMO_BRAND_DIR)), ("output_dir", "/tmp/x"),
                       ("templates_dir", "/tmp"), ("photo_dirs", ["/"])):
        row = Connection(id=_id(78), created_at=clock.now(), tenant_id=T, capability=Capability.IMAGE,
                         provider="template_render", mode=ConnectionMode.LOCAL, settings={key: value})
        p = tr.create(ProviderContext(T, row, clock))
        assert p.renderer is not None and Path(p.renderer.binary) == good  # operator configuration only
        assert p.health().connected is False and key in p.health().detail
        with pytest.raises(ProviderUnavailable):
            p.estimate(req())
        with pytest.raises(ProviderUnavailable):
            p.run(req())
    assert not marker.exists()


def test_add_rejects_a_chromium_setting(clock, brand_root):
    registry = _registry(clock)
    for key in ("chromium", "brand_dir", "templates_dir", "output_dir", "photo_dirs"):
        with pytest.raises(InvalidConnection):
            registry.add(A, Capability.IMAGE, "template_render", settings={key: "/bin/sh"})


# ---------------------------------------------------------------------- fix 2: operator-rooted paths

def test_unset_root_is_unavailable_for_every_tenant(clock):
    for tenant, mocks in ((A, ()), (T, {T})):
        p = TemplateRender(clock, TemplateRenderSettings(), FakeRenderer(), tenant_id=tenant, mock_tenants=mocks,
                           environ={})
        h = p.health()
        assert h.connected is False and "THALLO_BRAND_ROOT" in h.detail
        with pytest.raises(ProviderUnavailable, match="THALLO_BRAND_ROOT"):
            p.estimate(image(tenant))
        with pytest.raises(ProviderUnavailable, match="THALLO_BRAND_ROOT"):
            p.run(image(tenant))
        assert p.renderer.calls == []


def test_unset_root_falls_through_to_manual(monkeypatch, tmp_path, clock):
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("THALLO_CHROMIUM", str(fake))
    monkeypatch.delenv("THALLO_BRAND_ROOT", raising=False)
    registry = _registry(clock)
    row = registry.add(A, Capability.IMAGE, "template_render")
    served = registry.run(A, image(A))
    assert served.provider == "manual" and served.skipped == ((row.id, "unavailable"),)


def test_contract_suite_with_the_root_set(clock, brand_root):
    p = TemplateRender(clock, TemplateRenderSettings(), FakeRenderer(), **demo(brand_root))
    assert_provider_contract(p, clock, requests=[req("quote-01")])


@pytest.mark.parametrize("root", ["relative/brands", "", "/no/such/folder/anywhere"])
def test_root_must_be_an_existing_absolute_folder(root):
    with pytest.raises(BrandMisconfigured):
        tenant_paths(A, {"THALLO_BRAND_ROOT": root})


@pytest.mark.parametrize("tenant", ["..", ".", "../" + B, B + "/../" + A, "/etc", "a/b", "", "a\x00b", "a b", None])
def test_tenant_id_that_is_not_a_plain_folder_name_is_refused(brand_root, tenant):
    with pytest.raises(BrandMisconfigured, match="plain folder name"):
        tenant_paths(tenant, {"THALLO_BRAND_ROOT": str(brand_root)})


def test_traversal_through_the_provider_is_refused(brand_root, clock):
    real_brand(brand_root, B, "bravo")
    p = real(brand_root, "../" + B)
    assert p.health().connected is False
    with pytest.raises(ProviderUnavailable):
        p.estimate(image("../" + B))


def test_each_tenant_resolves_only_its_own_brand(brand_root):
    real_brand(brand_root, A, "alpha")
    real_brand(brand_root, B, "bravo")
    a, b = real(brand_root, A), real(brand_root, B)
    assert a.health().connected and b.health().connected
    assert a.run(image(A)).provenance.settings["brand"] == "alpha"
    assert b.run(image(B)).provenance.settings["brand"] == "bravo"
    out_a = Path(a.run(image(A)).output.storage_uri.removeprefix("file://"))
    assert out_a.parent == brand_root.resolve() / A / "renders"


def test_tenant_a_cannot_resolve_tenant_bs_brand(brand_root):
    real_brand(brand_root, B, "bravo")
    a = real(brand_root, A)  # A has no brand folder; B does
    assert a.health().connected is False
    with pytest.raises(ProviderUnavailable, match="no brand folder"):
        a.run(image(A))
    with pytest.raises(InvalidConnection, match="another tenant"):
        a.estimate(image(B))  # A's connection asked to serve B's request
    with pytest.raises(InvalidConnection, match="another tenant"):
        a.run(image(B))
    assert a.renderer.calls == []


def test_tenant_folder_that_is_a_symlink_to_another_tenant_is_refused(brand_root):
    real_brand(brand_root, B, "bravo")
    (brand_root / A).symlink_to(brand_root / B)
    a = real(brand_root, A)
    assert a.health().connected is False and "plain directory" in a.health().detail
    with pytest.raises(ProviderUnavailable):
        a.run(image(A))


@pytest.mark.parametrize("sub", ["brand", "templates", "photos"])
def test_sub_folder_symlink_out_of_the_tenant_folder_is_refused(brand_root, tmp_path, sub):
    real_brand(brand_root, B, "bravo")
    (brand_root / A).mkdir()
    if sub == "brand":
        (brand_root / A / "brand").symlink_to(brand_root / B / "brand")  # into another tenant
    else:
        outside = tmp_path / f"outside-{sub}"
        outside.mkdir()
        (brand_root / A / sub).symlink_to(outside)  # anywhere at all outside A
    with pytest.raises(BrandMisconfigured, match="outside the tenant folder"):
        tenant_paths(A, {"THALLO_BRAND_ROOT": str(brand_root)})
    a = real(brand_root, A)
    assert a.health().connected is False
    with pytest.raises(ProviderUnavailable):
        a.run(image(A))


def test_symlink_inside_the_tenant_folder_is_allowed(brand_root):
    real_brand(brand_root, A, "alpha")
    (brand_root / A / "shared").mkdir()
    (brand_root / A / "photos").symlink_to(brand_root / A / "shared")
    paths = tenant_paths(A, {"THALLO_BRAND_ROOT": str(brand_root)})
    assert paths.photos == (brand_root / A / "shared").resolve()


def test_photos_come_only_from_the_tenants_own_folders(brand_root, tmp_path):
    real_brand(brand_root, A, "alpha")
    real_brand(brand_root, B, "bravo")
    for t in (A, B):
        (brand_root / t / "photos").mkdir()
        (brand_root / t / "photos" / "shop.png").write_bytes(PNG)
    stray = tmp_path / "stray.png"
    stray.write_bytes(PNG)
    a = real(brand_root, A)
    own = a.run(image(A, "quote-04", quote="Hi", attribution="X", photo=str(brand_root / A / "photos" / "shop.png")))
    assert own.output.sha256
    for bad in (str(brand_root / B / "photos" / "shop.png"),  # another tenant's photo, absolute
                str(brand_root / A / "photos" / ".." / ".." / B / "photos" / "shop.png"),  # traversal
                str(stray)):  # an arbitrary absolute path
        with pytest.raises(RenderRejected, match="outside the allowed photo folders"):
            a.run(image(A, "quote-04", quote="Hi", attribution="X", photo=bad))


def test_fictional_flag_is_read_from_operator_data(brand_root):
    """The gate trusts `fictional` in the brand's own tokens file. That file now always sits
    under the operator's root (no setting can point at a tenant-made folder), and a real tenant
    whose operator folder holds a fictional brand is still refused."""
    d = brand_root / A / "brand"
    shutil.copytree(DEMO_BRAND_DIR, d)  # the demo, fictional: true
    a = real(brand_root, A)
    assert a.health().connected is False and "fictional" in a.health().detail
    with pytest.raises(ProviderUnavailable, match="fictional"):
        a.run(image(A))
