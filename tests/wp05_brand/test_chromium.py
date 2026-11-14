"""Real renders through headless Chromium. Skipped, with the reason, where no browser exists.

Set THALLO_FULL_MATRIX=1 to lint every template at every channel size in every language
(3,456 renders measured in one batch, about two minutes)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
from pathlib import Path

import pytest

from thallo.brand.tokens import DEMO_BRAND_DIR, load_brand
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Language
from thallo.contracts.testing.contract_suite import assert_provider_contract
from thallo.media.lint import RenderRejected, lint_layout, lint_static, parse_layout
from thallo.media.render import batch_viewport, compose, plan_job
from thallo.providers.image.template_render import TemplateRender, TemplateRenderSettings

from .conftest import DEMO, req


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def _file(result) -> bytes:
    return Path(result.output.storage_uri.removeprefix("file://")).read_bytes()


@pytest.fixture
def provider(chromium, tmp_path):
    return TemplateRender(FakeClock(FAKE_EPOCH), TemplateRenderSettings(output_dir=str(tmp_path / "out")), chromium,
                          **DEMO)


def _lint_batch(chromium, brand, book, requests):
    jobs = [plan_job(r, brand=brand, book=book) for r in requests]
    layout = parse_layout(chromium.measure(compose(jobs, batch=True), *batch_viewport(jobs)))
    bad, i = [], 0
    for j in jobs:
        idx = list(range(i, i + len(j.slides)))
        i += len(j.slides)
        rep = lint_static(j).merged(lint_layout(j, layout, idx))
        bad += [(j.template.ref, j.width, j.height, j.language.value, f.check, f.reason) for f in rep.failures()]
    return bad


def _pairs(book):
    return sorted({(ch.value, s.w, s.h) for ch, rule in book.channels.channels.items() for s in rule.sizes})


def _request(ref, ch, w, h, lang):
    extra = {"_output": "pdf"} if ref.startswith("carousel") else {}
    return req(ref, w, h, _lang=lang, _channel=ch, **extra)


def test_every_template_in_every_language_passes_lint(chromium, brand, book):
    reqs = [_request(ref, "instagram", 1080, 1350, lang.value) for ref in book.refs() for lang in Language]
    assert _lint_batch(chromium, brand, book, reqs) == []


def test_every_family_at_every_channel_size_passes_lint(chromium, brand, book):
    reqs = [_request(f"{fam}-{n:02d}", ch, w, h, lang)
            for fam in book.families for (ch, w, h) in _pairs(book)
            for n, lang in ((1, "en"), (5, "ta"), (8, "it"))]
    assert _lint_batch(chromium, brand, book, reqs) == []


@pytest.mark.skipif(os.environ.get("THALLO_FULL_MATRIX") != "1", reason="full matrix: set THALLO_FULL_MATRIX=1")
def test_full_matrix(chromium, brand, book):
    reqs = [_request(ref, ch, w, h, lang.value) for ref in book.refs() for (ch, w, h) in _pairs(book)
            for lang in Language]
    assert _lint_batch(chromium, brand, book, reqs) == []


def test_every_channel_size_renders_a_png_of_that_size(provider, book):
    fams = [f for f in book.families if f != "carousel"]
    channel_for = {(w, h): ch for ch, w, h in _pairs(book)}
    for i, (w, h) in enumerate(book.channels.all_sizes()):
        res = provider.run(req(f"{fams[i % len(fams)]}-{i % 8 + 1:02d}", w, h, _lang=("en", "it", "ta")[i % 3],
                               _channel=channel_for[(w, h)]))
        data = _file(res)
        assert _png_size(data) == (w, h)
        assert hashlib.sha256(data).hexdigest() == res.output.sha256


def test_pdf_has_one_page_per_slide(provider):
    slots = {"s1.headline": "First", "s2.headline": "Second", "s2.body": "Body text.", "s3.headline": "Third",
             "_output": "pdf"}
    res = provider.run(req("carousel-02", 1080, 1350, **slots))
    data = _file(res)
    assert data.startswith(b"%PDF")
    assert data.count(b"/Type /Page") - data.count(b"/Type /Pages") == 3
    single = _file(provider.run(req("offer-01", _output="pdf")))
    assert single.count(b"/Type /Page") - single.count(b"/Type /Pages") == 1


def test_carousel_slide_as_png(provider):
    slots = {"s1.headline": "First", "s2.headline": "Second", "_slide": "2"}
    assert _png_size(_file(provider.run(req("carousel-01", **slots)))) == (1080, 1350)


def test_same_request_same_bytes(provider):
    a = provider.run(req("quote-03", quote="Hello there", attribution="A"))
    b = provider.run(req("quote-03", quote="Hello there", attribution="A"))
    assert a.output.sha256 == b.output.sha256


def test_tamil_renders_in_the_tamil_brand_font(chromium, brand, book):
    job = plan_job(req("quote-01", _lang="ta"), brand=brand, book=book)
    layout = parse_layout(chromium.measure(compose([job]), job.width, job.height))
    assert layout.fonts["thallo-tamil-body"] and layout.fonts["thallo-tamil-display"]
    families = {b.family for b in layout.slides[0].boxes if b.kind == "text" and b.visible}
    assert families <= {"thallo-tamil-display", "thallo-tamil-body"}


def test_real_overflow_fails(provider):
    with pytest.raises(RenderRejected, match="design.overflow"):
        provider.run(req("single-02", 1080, 1080, headline="Precipitevolissimevolmente"))


def _brand_copy(tmp_path):
    d = tmp_path / "brand"
    shutil.copytree(DEMO_BRAND_DIR, d)
    return d


def test_real_low_contrast_fails(chromium, tmp_path):
    d = _brand_copy(tmp_path)
    data = json.loads((d / "demo.brand.json").read_text())
    data["colors"]["muted"] = "#E8E0D4"  # nearly the paper colour
    (d / "demo.brand.json").write_text(json.dumps(data))
    p = TemplateRender(FakeClock(FAKE_EPOCH), TemplateRenderSettings(brand_dir=str(d), output_dir=str(tmp_path / "o")),
                       chromium, **DEMO)
    with pytest.raises(RenderRejected, match="design.contrast") as exc:
        p.run(req("quote-01", quote="Hello", attribution="Someone"))
    assert "attribution" in str(exc.value)
    assert load_brand(d).tokens.fictional


def test_real_missing_font_fails(chromium, tmp_path):
    d = _brand_copy(tmp_path)
    path = d / "script" / "tamil.json"
    data = json.loads(path.read_text())
    data["fonts"]["body"]["local"] = ["No Such Font Thallo"]
    path.write_text(json.dumps(data))
    p = TemplateRender(FakeClock(FAKE_EPOCH), TemplateRenderSettings(brand_dir=str(d), output_dir=str(tmp_path / "o")),
                       chromium, **DEMO)
    with pytest.raises(RenderRejected, match="design.font_loaded"):
        p.run(req("quote-01", _lang="ta"))


def test_contract_suite_with_default_sample(provider):
    """The shared sample (quote-01, no slots) is a template preview."""
    assert_provider_contract(provider, FakeClock(FAKE_EPOCH))


@pytest.mark.parametrize("slots", [
    {"_output": "pdf"},
    {"s1.headline": "First", "s2.headline": "Second", "s3.headline": "Third", "_output": "pdf"},
], ids=["single", "carousel"])
def test_pdf_hash_survives_a_later_re_render(chromium, tmp_path, slots):
    """Chromium stamps PDFs with the wall-clock time. A re-render later, with the wall clock and
    the injected clock both moved on, must give the same bytes, so an approved hash holds."""
    import time
    from datetime import timedelta
    clock = FakeClock(FAKE_EPOCH)
    p = TemplateRender(clock, TemplateRenderSettings(output_dir=str(tmp_path / "o")), chromium, **DEMO)
    ref = "carousel-02" if "s1.headline" in slots else "offer-01"
    first = p.run(req(ref, **slots))
    time.sleep(1.2)  # Chromium's PDF dates have one-second resolution
    clock.advance(timedelta(days=3))
    second = p.run(req(ref, **slots))
    assert first.output.sha256 == second.output.sha256
    data = _file(second)
    assert b"(D:20000101000000" in data and hashlib.sha256(data).hexdigest() == second.output.sha256


def test_output_run_reports_its_own_fonts(chromium, brand, book, tmp_path):
    """The run that writes the bytes reports the fonts it loaded; a font that is not on the
    machine shows as fallen back in that same run."""
    job = plan_job(req("quote-01", _lang="ta"), brand=brand, book=book)
    out = chromium.png(compose([job]), job.width, job.height)
    assert out.data.startswith(b"\x89PNG") and out.fonts and all(out.fonts.values())
    d = _brand_copy(tmp_path)
    path = d / "script" / "tamil.json"
    data = json.loads(path.read_text())
    data["fonts"]["body"]["local"] = ["No Such Font Thallo"]
    path.write_text(json.dumps(data))
    broken = plan_job(req("quote-01", _lang="ta"), brand=load_brand(d), book=book)
    fonts = chromium.pdf(compose([broken]), broken.width, broken.height).fonts
    assert fonts is not None and fonts["thallo-tamil-body"] is False
