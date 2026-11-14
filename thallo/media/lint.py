"""Design lint, in code (BUILD_PLAN 2.7). Layer CODE. Every verdict is PASS or FAIL, never FLAG.

Two halves, both plain functions with no browser:

- `lint_static(job)` reads the plan: unknown, missing or overlong slots, words per slide, the
  script of the text against the language, and the canvas size against the channel.
- `lint_layout(job, layout)` reads what the browser measured: fonts that did not load, a text
  box in the wrong script's font, contrast, minimum font size, overflow, safe zone, logo clear
  space and logo size.

Contrast is computed from the colours stacked under five points of each text box. Where a photo
sits under the text, its colour is unknown, so the check composites every layer above it over
both pure black and pure white and keeps the worse ratio.

`to_check_results` turns a report into `check_result` rows, one per check.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from thallo.brand.tokens import script_for
from thallo.contracts.clock import Clock
from thallo.contracts.enums import CheckLayer, CheckVerdict, Language
from thallo.contracts.ids import new_id
from thallo.contracts.models import CheckResult
from thallo.media.render import Job, font_alias, safe_insets

CHECKS = (
    "design.request", "design.slots", "design.words_per_slide", "design.script", "design.canvas_size",
    "design.font_loaded", "design.contrast", "design.min_font_size", "design.overflow", "design.safe_zone",
    "design.logo_clear_space", "design.logo_size",
)
STATIC_CHECKS = CHECKS[:5]
TOLERANCE_PX = 1.0
_MAX_REASONS = 6

RGBA = tuple[float, float, float, float]
BLACK: RGBA = (0.0, 0.0, 0.0, 1.0)
WHITE: RGBA = (255.0, 255.0, 255.0, 1.0)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Finding(_Frozen):
    check: str
    verdict: CheckVerdict
    reason: str | None = None
    slide: int | None = None


class LintReport(_Frozen):
    findings: tuple[Finding, ...]

    @property
    def failed(self) -> bool:
        return any(f.verdict is CheckVerdict.FAIL for f in self.findings)

    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict is CheckVerdict.FAIL]

    def summary(self) -> dict[str, tuple[CheckVerdict, str | None]]:
        """One verdict per check: FAIL if any finding failed, with the first reasons joined."""
        out: dict[str, tuple[CheckVerdict, str | None]] = {}
        for name in CHECKS:
            mine = [f for f in self.findings if f.check == name]
            if not mine:
                continue
            bad = [f for f in mine if f.verdict is CheckVerdict.FAIL]
            if bad:
                reasons = [(f"slide {f.slide}: " if f.slide else "") + (f.reason or "") for f in bad]
                more = f" (+{len(reasons) - _MAX_REASONS} more)" if len(reasons) > _MAX_REASONS else ""
                out[name] = (CheckVerdict.FAIL, "; ".join(reasons[:_MAX_REASONS]) + more)
            else:
                out[name] = (CheckVerdict.PASS, None)
        return out

    def as_dicts(self) -> list[dict[str, str | None]]:
        return [{"check": k, "verdict": v.value, "reason": r} for k, (v, r) in self.summary().items()]

    def merged(self, other: LintReport) -> LintReport:
        return LintReport(findings=self.findings + other.findings)


def _ok(check: str) -> Finding:
    return Finding(check=check, verdict=CheckVerdict.PASS)


def _fail(check: str, reason: str, slide: int | None = None) -> Finding:
    return Finding(check=check, verdict=CheckVerdict.FAIL, reason=reason, slide=slide)


class RenderRejected(Exception):
    """The render failed the design lint, so no file was kept and nothing reaches a person.
    The caller writes `to_check_results(exc.report, ...)`: FAIL rows in layer CODE."""

    def __init__(self, report: LintReport) -> None:
        self.report = report
        fails = [f"{k}: {r}" for k, (v, r) in report.summary().items() if v is CheckVerdict.FAIL]
        super().__init__("design lint failed: " + " | ".join(fails))


def request_failure(reason: str) -> LintReport:
    return LintReport(findings=(_fail("design.request", reason),))


# ====================================================================== static half

def _letter_scripts(text: str) -> set[str]:
    out = set()
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        out.add(name.split(" ", 1)[0] if name else "UNKNOWN")
    return out


_ALLOWED_LETTERS = {"latin": {"LATIN"}, "tamil": {"TAMIL", "LATIN"}}


def lint_static(job: Job) -> LintReport:
    found: list[Finding] = []
    fam = job.template.family
    st = job.script
    if fam.carousel and len(job.slides) < fam.min_slides and job.output == "pdf":
        found.append(_fail("design.slots", f"a carousel needs at least {fam.min_slides} slides"))
    has_tamil = False
    for slide in job.slides:
        n = slide.number if fam.carousel else None
        unknown = sorted(set(slide.slots) - set(fam.slots))
        if unknown:
            found.append(_fail("design.slots", f"unknown slots {unknown}", n))
        for name in fam.slots:
            spec = job.template.slot_spec(name)
            if spec.role == "photo":
                if spec.required and slide.photo is None:
                    found.append(_fail("design.slots", f"layout {job.template.ref} needs a photo", n))
                continue
            text = (slide.slots.get(name) or "").strip()
            if spec.required and not text:
                found.append(_fail("design.slots", f"{name} is required", n))
                continue
            if spec.max_chars and len(text) > spec.max_chars * st.char_factor:
                found.append(_fail("design.slots", f"{name} has {len(text)} characters, limit "
                                   f"{int(spec.max_chars * st.char_factor)}", n))
            if spec.max_words and len(text.split()) > spec.max_words:
                found.append(_fail("design.slots", f"{name} has {len(text.split())} words, limit {spec.max_words}", n))
        words = sum(len((slide.slots.get(k) or "").split()) for k, s in fam.slots.items() if s.role == "text")
        if words > fam.max_words_per_slide:
            found.append(_fail("design.words_per_slide", f"{words} words, limit {fam.max_words_per_slide}", n))
        for name, text in slide.slots.items():
            scripts = _letter_scripts(text)
            has_tamil = has_tamil or "TAMIL" in scripts
            stray = scripts - _ALLOWED_LETTERS[st.script]
            if stray:
                found.append(_fail("design.script", f"{name} has {sorted(stray)} letters in a "
                                   f"{job.language.value} render set in {st.script} fonts", n))
    if job.language is Language.TA and not has_tamil:
        found.append(_fail("design.script", "language ta but no Tamil text in any slot"))
    if script_for(job.language) != st.script:
        found.append(_fail("design.script", f"{job.language.value} needs {script_for(job.language)} tokens"))
    _, _, listed = job.book.channels.rule(job.channel, job.width, job.height)
    if job.channel is not None and not listed:
        found.append(_fail("design.canvas_size", f"{job.width}x{job.height} is not a {job.channel.value} size"))
    for check in STATIC_CHECKS:
        if check != "design.request" and not any(f.check == check for f in found):
            found.append(_ok(check))
    return LintReport(findings=tuple(found))


# ====================================================================== the measured layout

class Layer(_Frozen):
    c: str
    img: bool = False


class Box(_Frozen):
    name: str
    kind: str
    rect: tuple[float, float, float, float]  # x, y, w, h relative to the slide
    visible: bool
    empty: bool = False
    font_px: float | None = None
    family: str | None = None
    color: str | None = None
    overflow_x: bool = False
    overflow_y: bool = False
    stacks: tuple[tuple[Layer, ...], ...] = ()  # topmost layer first, one stack per sample point


class Region(_Frozen):
    name: str
    rect: tuple[float, float, float, float]
    overflow_x: bool = False
    overflow_y: bool = False


class SlideLayout(_Frozen):
    index: int
    number: int = 1
    script: str
    rect: tuple[float, float, float, float]
    boxes: tuple[Box, ...]
    regions: tuple[Region, ...] = ()


class Layout(_Frozen):
    fonts: dict[str, bool]
    slides: tuple[SlideLayout, ...] = Field(default=())


def parse_layout(data: dict) -> Layout:
    return Layout.model_validate(data)


# ---------------------------------------------------------------------- colour

_RGB_RE = re.compile(r"^rgba?\(\s*([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$")
_SRGB_RE = re.compile(r"^color\(\s*srgb\s+([-\d.e]+%?)\s+([-\d.e]+%?)\s+([-\d.e]+%?)(?:\s*/\s*([\d.]+%?))?\s*\)$")


class ColorError(ValueError):
    """A computed colour this lint cannot read. The contrast check records it as a FAIL."""


def _unit(v: str) -> float:
    """A `color(srgb ...)` channel: 0..1, or a percentage."""
    x = float(v[:-1]) / 100 if v.endswith("%") else float(v)
    return min(max(x, 0.0), 1.0)


def _alpha(a: str | None) -> float:
    if a is None:
        return 1.0
    return min(max(float(a[:-1]) / 100 if a.endswith("%") else float(a), 0.0), 1.0)


def parse_color(value: str) -> RGBA:
    v = value.strip().lower()
    if v == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if re.fullmatch(r"#[0-9a-f]{6}", v):
        return (int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16), 1.0)
    try:
        if m := _RGB_RE.match(v):
            return (float(m[1]), float(m[2]), float(m[3]), _alpha(m[4]))
        if m := _SRGB_RE.match(v):
            return (_unit(m[1]) * 255, _unit(m[2]) * 255, _unit(m[3]) * 255, _alpha(m[4]))
    except ValueError:
        pass
    raise ColorError(f"cannot read colour {value!r}")


def over(top: RGBA, base: RGBA) -> RGBA:
    """Source-over compositing onto an opaque base."""
    a = top[3]
    return (top[0] * a + base[0] * (1 - a), top[1] * a + base[1] * (1 - a), top[2] * a + base[2] * (1 - a), 1.0)


def luminance(c: RGBA) -> float:
    def ch(v: float) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def contrast_ratio(a: RGBA, b: RGBA) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def backgrounds(stack: Iterable[Layer]) -> list[RGBA]:
    """Possible opaque backgrounds under the text, given the layers topmost first. A photo or
    other image makes the result both black and white, worst case, before the layers above it."""
    bases: list[RGBA] = [WHITE]
    for layer in reversed(tuple(stack)):
        if layer.img:
            bases = [BLACK, WHITE]
            continue
        col = parse_color(layer.c)
        if col[3] > 0:
            bases = [over(col, b) for b in bases]
    return bases


def worst_contrast(color: str, stacks: Iterable[Iterable[Layer]]) -> float:
    text = parse_color(color)
    worst = float("inf")
    for stack in stacks:
        for base in backgrounds(stack):
            worst = min(worst, contrast_ratio(over(text, base), base))
    return worst


# ---------------------------------------------------------------------- geometry

def _inside(r: tuple[float, ...], area: tuple[float, float, float, float]) -> bool:
    x, y, w, h = r
    ax0, ay0, ax1, ay1 = area
    return x >= ax0 - TOLERANCE_PX and y >= ay0 - TOLERANCE_PX and x + w <= ax1 + TOLERANCE_PX \
        and y + h <= ay1 + TOLERANCE_PX


def _intersects(a: tuple[float, ...], b: tuple[float, float, float, float]) -> bool:
    x, y, w, h = a
    bx0, by0, bx1, by1 = b
    return x < bx1 - TOLERANCE_PX and x + w > bx0 + TOLERANCE_PX and y < by1 - TOLERANCE_PX \
        and y + h > by0 + TOLERANCE_PX


Area = tuple[float, float, float, float]  # x0, y0, x1, y1


def _lint_text(job: Job, b: Box, n: int | None, frame: tuple[float, ...] | None, safe_area: Area,
               aliases: dict[str, str]) -> list[Finding]:
    found: list[Finding] = []
    tokens = job.brand.tokens
    min_font_1080, _, _ = job.book.channels.rule(job.channel, job.width, job.height)
    min_px = min_font_1080 * job.scale
    if b.overflow_x or b.overflow_y:
        found.append(_fail("design.overflow", f"{b.name} is clipped", n))
    elif frame and not _inside(b.rect, (frame[0], frame[1], frame[0] + frame[2], frame[1] + frame[3])):
        found.append(_fail("design.overflow", f"{b.name} runs outside its frame", n))
    if b.family not in aliases:
        found.append(_fail("design.script", f"{b.name} is set in {b.family}, not a {job.script.script} brand font", n))
    if b.font_px is None or b.font_px + 0.01 < min_px:
        found.append(_fail("design.min_font_size", f"{b.name} is {b.font_px or 0:.1f}px, minimum {min_px:.1f}px", n))
    if b.color is None or not b.stacks:
        found.append(_fail("design.contrast", f"{b.name} has no measured colours", n))
    else:
        large = (b.font_px or 0) >= tokens.contrast.large_text_px * job.scale
        need = tokens.contrast.large if large else tokens.contrast.normal
        try:
            ratio = worst_contrast(b.color, b.stacks)
        except ColorError as exc:
            found.append(_fail("design.contrast", f"{b.name}: {exc}", n))
        else:
            if ratio + 1e-9 < need:
                found.append(_fail("design.contrast", f"{b.name} contrast {ratio:.2f}, needs {need:g}", n))
    if not _inside(b.rect, safe_area):
        found.append(_fail("design.safe_zone", f"{b.name} is outside the safe zone", n))
    return found


def _lint_logos(job: Job, logos: list[Box], texts: list[Box], n: int | None, safe_area: Area) -> list[Finding]:
    logo = job.brand.tokens.logo
    if not logos:
        return [_fail("design.logo_size", "no logo on the slide", n)]
    found: list[Finding] = []
    for lg in logos:
        x, y, w, h = lg.rect
        if w / job.scale + 0.01 < logo.min_width:
            found.append(_fail("design.logo_size", f"logo {w / job.scale:.0f}px wide at 1080, minimum "
                               f"{logo.min_width:g}", n))
        if not _inside(lg.rect, safe_area):
            found.append(_fail("design.safe_zone", "logo is outside the safe zone", n))
        pad = logo.clear_space * h
        zone = (x - pad, y - pad, x + w + pad, y + h + pad)
        found += [_fail("design.logo_clear_space", f"{b.name} is inside the logo's clear space", n)
                  for b in texts if _intersects(b.rect, zone)]
    return found


def lint_layout(job: Job, layout: Layout, slide_indexes: Iterable[int] | None = None) -> LintReport:
    """Lint the measured slides of one job. `slide_indexes` picks this job's slides out of a
    batch document; by default every slide in the layout belongs to the job."""
    found: list[Finding] = []
    st = job.script
    wanted = list(slide_indexes) if slide_indexes is not None else [s.index for s in layout.slides]
    slides = [s for s in layout.slides if s.index in wanted]
    if len(slides) != len(job.slides):
        found.append(_fail("design.overflow", f"measured {len(slides)} slides, expected {len(job.slides)}"))
    aliases = {font_alias(st.script, role): face.family for role, face in st.fonts.items()}
    missing = [fam for alias, fam in aliases.items() if not layout.fonts.get(alias, False)]
    if missing:
        found.append(_fail("design.font_loaded", f"fonts not installed on the renderer: {sorted(missing)}"))
    top, right, bottom, left = safe_insets(job)
    safe_area = (left, top, job.width - right, job.height - bottom)
    for sl in slides:
        n = sl.number if job.template.family.carousel else None
        if abs(sl.rect[2] - job.width) > TOLERANCE_PX or abs(sl.rect[3] - job.height) > TOLERANCE_PX:
            found.append(_fail("design.canvas_size", f"slide measured {sl.rect[2]:g}x{sl.rect[3]:g}", n))
        texts = [b for b in sl.boxes if b.kind == "text" and b.visible and not b.empty]
        for r in sl.regions:
            if r.overflow_x or r.overflow_y:
                found.append(_fail("design.overflow", f"text does not fit the {r.name}", n))
        frame = next((r.rect for r in sl.regions if r.name == "frame"), None)
        for b in texts:
            found += _lint_text(job, b, n, frame, safe_area, aliases)
        found += _lint_logos(job, [b for b in sl.boxes if b.kind == "logo" and b.visible], texts, n, safe_area)
    for check in CHECKS[5:] + ("design.canvas_size", "design.script", "design.overflow"):
        if not any(f.check == check for f in found):
            found.append(_ok(check))
    return LintReport(findings=tuple(found))


def lint_output_fonts(job: Job, fonts: dict[str, bool] | None) -> LintReport:
    """The fonts as loaded in the browser run that wrote the shipped bytes. The layout lint
    reads the measuring run; this one holds the output run to the same rule. No report from
    that run, or a font that fell back, is a FAIL and the bytes are not kept."""
    st = job.script
    aliases = {font_alias(st.script, role): face.family for role, face in st.fonts.items()}
    if fonts is None:
        return LintReport(findings=(_fail("design.font_loaded", "the output run did not report its fonts"),))
    missing = [fam for alias, fam in aliases.items() if not fonts.get(alias, False)]
    if missing:
        return LintReport(findings=(_fail("design.font_loaded",
                                          f"fonts fell back in the output run: {sorted(missing)}"),))
    return LintReport(findings=(_ok("design.font_loaded"),))


# ====================================================================== rows

def to_check_results(report: LintReport, *, tenant_id: str, variant_id: str, content_sha256: str, clock: Clock,
                     attempt: int = 1) -> list[CheckResult]:
    """One `check_result` row per check in layer CODE. PASS or FAIL only. `content_sha256` is the
    hash of the variant content the rendered media belongs to (`thallo.gate.variant_hash`, or
    `thallo.gate.content_hashes` once the media is linked); the gate counts the rows only while the
    variant still hashes to it."""
    rows = []
    for name, (verdict, reason) in report.summary().items():
        if verdict is CheckVerdict.FLAG:  # pragma: no cover - the lint has no FLAG path
            raise AssertionError("design lint never flags")
        rows.append(CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id,
                                variant_id=variant_id, layer=CheckLayer.CODE, check_name=name,
                                verdict=verdict, reason=reason, attempt=attempt, content_sha256=content_sha256))
    return rows
