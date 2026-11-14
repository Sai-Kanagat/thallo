from __future__ import annotations

import hashlib
import json
import re

import pytest

from thallo.brand.templates import default_templates
from thallo.brand.tokens import DEMO_BRAND_DIR, load_brand
from thallo.contracts import connection as c
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.media.render import ChromiumRenderer, Rendered, find_chromium, plan_job

T = "0190f3a0-0000-7000-8000-000000000001"
V = "0190f3a0-0000-7000-8000-0000000000a1"
DEMO = {"tenant_id": T, "mock_tenants": {T}, "environ": {}}  # T is a mock (demo) tenant
NO_CHROMIUM = "no headless Chromium on this machine (set THALLO_CHROMIUM to one); browser tests skipped"


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture(scope="session")
def book():
    return default_templates()


@pytest.fixture(scope="session")
def brand():
    return load_brand(DEMO_BRAND_DIR)


@pytest.fixture(scope="session")
def chromium():
    binary = find_chromium()
    if binary is None:
        pytest.skip(NO_CHROMIUM)
    return ChromiumRenderer(binary, timeout_s=300)


def req(ref: str = "quote-01", w: int = 1080, h: int = 1350, **slots: str) -> c.ImageRequest:
    return c.ImageRequest(tenant_id=T, purpose="template_render", template_ref=ref, width=w, height=h,
                          slots=slots, idempotency_key=f"k-{ref}-{w}x{h}")


def job_for(brand, book, ref="quote-01", w=1080, h=1350, photo_roots=(), **slots):
    return plan_job(req(ref, w, h, **slots), brand=brand, book=book, photo_roots=photo_roots)


class FakeRenderer:
    """No browser. Measures a layout that passes (logo in the safe zone, fonts loaded, no text
    boxes) unless told otherwise, and returns bytes derived from the document."""

    name = "fake"

    def __init__(self, layout_patch=None, output_fonts=None):
        self.layout_patch = layout_patch
        self.output_fonts = output_fonts  # patch for the fonts the output run reports
        self.calls: list[str] = []

    def version(self) -> str:
        return "fake-1"

    def measure(self, document: str, width: int, height: int) -> dict:
        self.calls.append("measure")
        fonts = json.loads(re.search(r'data-fonts="([^"]*)"', document)[1].replace("&quot;", '"'))
        n = len(re.findall(r'<section class="slide', document))
        scripts = re.findall(r'data-script="(\w+)"', document)
        slides = [{"index": i, "number": i + 1, "script": scripts[i], "rect": [0, 0, width, height],
                   "boxes": [{"name": "logo", "kind": "logo", "visible": True,
                              "rect": [width * 0.5, height * 0.5, 200 * width / 1080, 50 * width / 1080]}],
                   "regions": [{"name": "frame", "rect": [100, 100, width - 200, height - 200]}]}
                  for i in range(n)]
        layout = {"fonts": dict.fromkeys(fonts, True), "slides": slides}
        return self.layout_patch(layout) if self.layout_patch else layout

    def _fonts(self, document: str):
        fonts = dict.fromkeys(json.loads(re.search(r'data-fonts="([^"]*)"', document)[1].replace("&quot;", '"')),
                              True)
        return self.output_fonts(fonts) if self.output_fonts else fonts

    def png(self, document: str, width: int, height: int) -> Rendered:
        self.calls.append("png")
        return Rendered(b"\x89PNG fake " + hashlib.sha256(document.encode()).digest(), self._fonts(document))

    def pdf(self, document: str, width: int, height: int) -> Rendered:
        self.calls.append("pdf")
        return Rendered(b"%PDF-1.4 fake " + hashlib.sha256(document.encode()).digest(), self._fonts(document))
