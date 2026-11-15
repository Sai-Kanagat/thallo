"""The design lint with no browser: static checks on the plan, layout checks on synthetic
measurements, colour maths, and check_result rows."""
from __future__ import annotations

import pytest

from thallo.contracts.enums import CheckLayer, CheckVerdict
from thallo.media.lint import (
    CHECKS,
    Layer,
    contrast_ratio,
    lint_layout,
    lint_static,
    parse_color,
    parse_layout,
    to_check_results,
    worst_contrast,
)
from thallo.media.render import PlanError, compose, safe_insets

from .conftest import T, V, job_for

TAMIL = "இது ஒரு மாதிரி உரை"


def fails(report, check):
    return [f for f in report.failures() if f.check == check]


def only_pass_or_fail(report):
    return all(f.verdict in (CheckVerdict.PASS, CheckVerdict.FAIL) for f in report.findings)


# ---------------------------------------------------------------------- static

def test_preview_passes_static(brand, book):
    for ref in book.refs():
        for lang in ("en", "it", "ta"):
            extra = {"_output": "pdf"} if ref.startswith("carousel") else {}
            rep = lint_static(job_for(brand, book, ref, _lang=lang, **extra))
            assert not rep.failed, (ref, lang, rep.failures())


@pytest.mark.parametrize(("slots", "check"), [
    ({"attribution": "Someone"}, "design.slots"),  # quote missing
    ({"quote": "x " * 37, "attribution": "A"}, "design.slots"),  # too many words
    ({"quote": "a" * 201, "attribution": "A"}, "design.slots"),  # too many characters
    ({"quote": "Fine", "attribution": "A", "subtitle": "?"}, "design.slots"),  # unknown slot
    ({"quote": TAMIL, "attribution": "A"}, "design.script"),  # Tamil in an English render
    ({"quote": "Привет мир", "attribution": "A"}, "design.script"),  # a script the fonts lack
])
def test_static_failures(brand, book, slots, check):
    rep = lint_static(job_for(brand, book, "quote-01", **slots))
    assert fails(rep, check), rep.failures()
    assert only_pass_or_fail(rep)


def test_words_per_slide(brand, book):
    long = " ".join(["word"] * 38)  # every slot inside its own limit, the slide over 45 words
    slots = {"s1.headline": "a b c d e f g h i j", "s1.body": long, "s2.headline": "end", "_output": "pdf"}
    rep = lint_static(job_for(brand, book, "carousel-01", **slots))
    assert fails(rep, "design.words_per_slide") and not fails(rep, "design.slots")
    assert fails(rep, "design.words_per_slide")[0].slide == 1


def test_tamil_needs_tamil_text_and_gets_longer_limits(brand, book):
    rep = lint_static(job_for(brand, book, "quote-01", _lang="ta", quote="Only English", attribution="A"))
    assert fails(rep, "design.script")
    long_tamil = ("மாதிரி " * 30).strip()  # 209 characters: over 200 for Latin, under 200 x 1.6
    rep = lint_static(job_for(brand, book, "quote-01", _lang="ta", quote=long_tamil, attribution="பெயர்"))
    assert not fails(rep, "design.slots")


def test_canvas_size_must_belong_to_the_channel(brand, book):
    rep = lint_static(job_for(brand, book, "quote-01", 1200, 627, _channel="instagram"))
    assert fails(rep, "design.canvas_size")
    assert not lint_static(job_for(brand, book, "quote-01", 1200, 627, _channel="linkedin_company")).failed


def test_photo_layout_without_photo_fails(brand, book):
    rep = lint_static(job_for(brand, book, "single-04", headline="Hello"))
    assert fails(rep, "design.slots")


@pytest.mark.parametrize(("ref", "slots", "match"), [
    ("nope-01", {}, "unknown template"),
    ("quote-01", {"_lang": "fr"}, "fr"),
    ("quote-01", {"_colour": "red"}, "reserved"),
    ("quote-01", {"_output": "gif"}, "png or pdf"),
    ("quote-01", {"_slide": "1"}, "only for carousels"),
    ("quote-01", {"s1.quote": "x"}, "only for carousels"),
    ("carousel-01", {"s1.headline": "a", "s3.headline": "b", "_output": "pdf"}, "no gap"),
    ("carousel-01", {"s1.headline": "a", "s2.headline": "b"}, "needs _slide"),
    ("carousel-01", {"s1.headline": "a", "s2.headline": "b", "_slide": "3"}, "from 1 to 2"),
    ("carousel-01", {"headline": "a"}, "s1.headline"),
    ("single-04", {"headline": "a", "photo": "/etc/hosts"}, "not an image"),
])
def test_plan_errors(brand, book, ref, slots, match):
    with pytest.raises(PlanError, match=match):
        job_for(brand, book, ref, **slots)


def test_photo_must_sit_in_an_allowed_folder(brand, book, tmp_path):
    inside = tmp_path / "photos" / "a.png"
    inside.parent.mkdir()
    inside.write_bytes(b"x")
    outside = tmp_path / "b.png"
    outside.write_bytes(b"x")
    with pytest.raises(PlanError, match="outside"):
        job_for(brand, book, "single-04", headline="a", photo=str(outside), photo_roots=[inside.parent])
    with pytest.raises(PlanError, match="outside"):
        job_for(brand, book, "single-04", headline="a", photo=str(inside.parent / ".." / "b.png"),
                photo_roots=[inside.parent])
    job = job_for(brand, book, "single-04", headline="a", photo=str(inside), photo_roots=[inside.parent])
    assert job.slides[0].photo == inside.resolve()


def test_compose_escapes_slot_text(brand, book):
    job = job_for(brand, book, "quote-01", quote='<img src="http://evil.invalid/x"> "hi"', attribution="A & B")
    doc = compose([job])
    assert "<img src=\"http://evil.invalid" not in doc
    assert "&lt;img src=&quot;http://evil.invalid/x&quot;&gt;" in doc
    assert "A &amp; B" in doc
    assert "Content-Security-Policy" in doc and "default-src 'none'" in doc
    assert "http://" not in doc.replace("http://evil.invalid", "").replace("http://www.w3.org", "")


# ---------------------------------------------------------------------- measured layout

PAPER = "rgb(251, 246, 238)"
INK = "rgb(31, 26, 23)"


def layout_for(job, **box_overrides):
    """A measured layout that passes: one headline box and the logo, inside the safe zone."""
    top, right, bottom, left = safe_insets(job)
    w, h = job.width, job.height
    fonts = {f"thallo-{s}-{r}": True for s in ("latin", "tamil") for r in ("display", "body")}
    box = {"name": "quote", "kind": "text", "visible": True, "rect": [left + 10, top + 200, 600, 120],
           "font_px": 56.0, "family": f"thallo-{job.script.script}-display", "color": INK,
           "stacks": [[{"c": "rgba(0, 0, 0, 0)"}, {"c": PAPER}]]}
    box.update(box_overrides)
    logo = {"name": "logo", "kind": "logo", "visible": True, "rect": [w - right - 200, h - bottom - 50, 200, 50]}
    return {"fonts": fonts, "slides": [{"index": 0, "number": 1, "script": job.script.script, "rect": [0, 0, w, h],
                                        "boxes": [box, logo], "regions": [{"name": "frame",
                                                                           "rect": [left, top, w - left - right,
                                                                                    h - top - bottom]}]}]}


@pytest.fixture
def job(brand, book):
    return job_for(brand, book, "quote-01", quote="Fine", attribution="A")


def test_synthetic_layout_passes(job):
    rep = lint_layout(job, parse_layout(layout_for(job)))
    assert not rep.failed, rep.failures()


@pytest.mark.parametrize(("override", "check"), [
    ({"color": "rgb(220, 214, 205)"}, "design.contrast"),
    ({"color": None}, "design.contrast"),
    ({"color": "#FFFFFF", "stacks": [[{"c": "rgba(0, 0, 0, 0)", "img": True}]]}, "design.contrast"),
    ({"font_px": 12.0}, "design.min_font_size"),
    ({"overflow_y": True}, "design.overflow"),
    ({"overflow_x": True}, "design.overflow"),
    ({"rect": [0, 10, 600, 120]}, "design.safe_zone"),
    ({"family": "Times"}, "design.script"),
])
def test_layout_failures(job, override, check):
    rep = lint_layout(job, parse_layout(layout_for(job, **override)))
    assert fails(rep, check), rep.failures()
    assert only_pass_or_fail(rep)


def test_photo_under_text_passes_with_the_brand_scrim(job):
    stack = [[{"c": "rgba(0, 0, 0, 0)"}, {"c": "rgba(20, 16, 14, 0.62)"}, {"c": "rgba(0, 0, 0, 0)", "img": True}]]
    rep = lint_layout(job, parse_layout(layout_for(job, color="rgb(255, 255, 255)", stacks=stack)))
    assert not fails(rep, "design.contrast")


def test_region_overflow_and_frame_escape(job):
    data = layout_for(job)
    data["slides"][0]["regions"][0]["overflow_y"] = True
    assert fails(lint_layout(job, parse_layout(data)), "design.overflow")
    frame = data["slides"][0]["regions"][0]
    frame["overflow_y"] = False
    frame["rect"] = [frame["rect"][0], frame["rect"][1], 300, 100]
    assert fails(lint_layout(job, parse_layout(data)), "design.overflow")


def test_logo_rules(job):
    data = layout_for(job)
    # 10 px under the text, inside the 25 px clear space
    data["slides"][0]["boxes"][1]["rect"] = [500, 200 + safe_insets(job)[0] + 130, 200, 50]
    assert fails(lint_layout(job, parse_layout(data)), "design.logo_clear_space")
    data = layout_for(job)
    data["slides"][0]["boxes"][1]["rect"][2] = 80
    assert fails(lint_layout(job, parse_layout(data)), "design.logo_size")
    data = layout_for(job)
    data["slides"][0]["boxes"].pop()
    assert fails(lint_layout(job, parse_layout(data)), "design.logo_size")


def test_missing_font_fails(job):
    data = layout_for(job)
    data["fonts"]["thallo-latin-body"] = False
    rep = lint_layout(job, parse_layout(data))
    assert fails(rep, "design.font_loaded")
    assert "Helvetica Neue" in fails(rep, "design.font_loaded")[0].reason


def test_canvas_and_slide_count(job):
    data = layout_for(job)
    data["slides"][0]["rect"] = [0, 0, 1000, 1350]
    assert fails(lint_layout(job, parse_layout(data)), "design.canvas_size")
    data["slides"] = []
    assert lint_layout(job, parse_layout(data)).failed


def test_empty_optional_slots_are_ignored(job):
    rep = lint_layout(job, parse_layout(layout_for(job, empty=True, font_px=1.0, color=None)))
    assert not rep.failed


# ---------------------------------------------------------------------- colour maths

def test_colour_maths():
    assert contrast_ratio(parse_color("#000000"), parse_color("#ffffff")) == pytest.approx(21.0)
    assert parse_color("rgba(10, 20, 30, 0.5)") == (10, 20, 30, 0.5)
    assert parse_color("rgb(10 20 30 / 50%)") == (10, 20, 30, 0.5)
    assert parse_color("transparent")[3] == 0
    with pytest.raises(ValueError):
        parse_color("hsl(1, 2%, 3%)")
    # a photo makes the worst case pure white under white text: ratio 1
    assert worst_contrast("rgb(255, 255, 255)", [[Layer(c="transparent", img=True)]]) == pytest.approx(1.0)


def test_srgb_colours_are_read():
    assert parse_color("color(srgb 1 0.5 0)") == pytest.approx((255, 127.5, 0, 1.0))
    assert parse_color("color(srgb 0 0 0 / 0.25)") == pytest.approx((0, 0, 0, 0.25))
    assert parse_color("color(srgb 100% 0% 50% / 50%)") == pytest.approx((255, 0, 127.5, 0.5))


@pytest.mark.parametrize(("override", "fail"), [
    ({"color": "color(srgb 0.12 0.1 0.09)"}, False),
    ({"color": "color(srgb 0.86 0.84 0.8)"}, True),  # light text on paper
    ({"color": "color(display-p3 0 0 0)"}, True),
    ({"color": "oklch(0.2 0.01 40)"}, True),
    ({"stacks": [[{"c": "color(display-p3 1 1 1)"}]]}, True),
])
def test_unreadable_or_srgb_colour_is_a_row_not_a_crash(job, override, fail):
    rep = lint_layout(job, parse_layout(layout_for(job, **override)))
    assert bool(fails(rep, "design.contrast")) is fail, rep.failures()
    assert only_pass_or_fail(rep)


def test_output_run_fonts(job):
    from thallo.media.lint import lint_output_fonts
    fonts = {f"thallo-{s}-{r}": True for s in ("latin", "tamil") for r in ("display", "body")}
    assert not lint_output_fonts(job, fonts).failed
    assert fails(lint_output_fonts(job, {**fonts, "thallo-latin-body": False}), "design.font_loaded")
    assert fails(lint_output_fonts(job, {}), "design.font_loaded")
    assert fails(lint_output_fonts(job, None), "design.font_loaded")


# ---------------------------------------------------------------------- rows

def test_check_result_rows(job, clock):
    data = layout_for(job, font_px=10.0)
    rep = lint_static(job).merged(lint_layout(job, parse_layout(data)))
    sha = "0f" * 32
    rows = to_check_results(rep, tenant_id=T, variant_id=V, content_sha256=sha, clock=clock, attempt=2)
    assert all(r.content_sha256 == sha for r in rows)
    names = [r.check_name for r in rows]
    assert len(names) == len(set(names)) and set(names) <= set(CHECKS)
    assert all(r.layer is CheckLayer.CODE and r.attempt == 2 and r.variant_id == V for r in rows)
    assert all(r.verdict in (CheckVerdict.PASS, CheckVerdict.FAIL) for r in rows)
    bad = [r for r in rows if r.verdict is CheckVerdict.FAIL]
    assert [r.check_name for r in bad] == ["design.min_font_size"] and "10.0px" in bad[0].reason
