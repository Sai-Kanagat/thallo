"""Render one sample of each template family with Sai's brand, so his proposed brand values can be judged by eye.

    python scripts/demo_sai_renders.py [out_dir]        default: docs/demo_screens/sai

Uses the real template renderer (headless Chromium) and the repository's own specimen wording; no database, no
network, no keys. Run it again after changing demo_brands/sai/brand.json or script/latin.json. The five demo
pieces come from `scripts/demo.sh --brand sai` itself, not from here.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from thallo.contracts import connection as c  # noqa: E402
from thallo.contracts.clock import SystemClock  # noqa: E402
from thallo.media.lint import RenderRejected  # noqa: E402
from thallo.media.render import ChromiumRenderer, find_chromium  # noqa: E402
from thallo.providers.image.template_render import TemplateRender  # noqa: E402

# Paper layouts only: Sai has no reversed logo yet, so the last sample shows what a dark layout does to the mark.
SAMPLES = (
    ("family-single", "single-01", {}),
    ("family-carousel", "carousel-01", {"_slide": "1"}),
    ("family-quote", "quote-01", {}),
    ("family-proof", "proof-01", {}),
    ("family-offer", "offer-02", {}),
    ("family-video-frame", "video_frame-08", {}),
    ("gap-dark-background", "single-02", {}),
)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "demo_screens" / "sai"
    chromium = find_chromium()
    if chromium is None:
        print("No headless Chromium found; set THALLO_CHROMIUM.")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    tenant = "sai-samples"
    with tempfile.TemporaryDirectory(prefix="thallo-sai-") as tmp:
        shutil.copytree(ROOT / "demo_brands" / "sai", Path(tmp) / tenant / "brand",
                        ignore=shutil.ignore_patterns("*.md", "claims.json", "brand.extras.json"))
        renderer = TemplateRender(SystemClock(), renderer=ChromiumRenderer(chromium), tenant_id=tenant,
                                  mock_tenants=(tenant,), environ={"THALLO_BRAND_ROOT": tmp})
        failed = 0
        for name, template, extra in SAMPLES:
            request = c.ImageRequest(tenant_id=tenant, purpose="template_render", template_ref=template,
                                     slots={**extra, "_lang": "en", "_channel": "linkedin_personal"},
                                     width=1080, height=1350)
            try:
                media = renderer.run(request).output
            except RenderRejected as exc:
                failed += 1
                print(f"{name}: refused by the design lint: {exc}")
                continue
            shutil.copy(Path(media.storage_uri.removeprefix("file://")), out / f"{name}.png")
            print(f"{name}: {template} rendered")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
