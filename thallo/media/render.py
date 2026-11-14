"""Template renderer (BUILD_PLAN 2.7 "Render", module 9).

Three steps, kept apart so the lint can be tested with no browser:

1. `plan_job` turns an `ImageRequest` into a `Job`: template, brand tokens for the script,
   language, channel, size, output format and the slot text for each slide.
2. `compose` writes one HTML document from the template fragment, the tokens and the escaped
   slot text. Several jobs can share one document (`batch=True`) so a test can measure every
   template at every size in one browser call.
3. A `Renderer` measures the document (boxes, fonts, colours under each text box) and writes
   the PNG or PDF. `ChromiumRenderer` drives a local headless Chromium through its command line:
   no network (every host name resolves to nothing), a throwaway profile, a hard timeout.

Reserved request slots, all optional: `_lang` (en, it, ta), `_channel` (a Channel value),
`_output` (png or pdf), `_slide` (carousel slide number for a single PNG). Carousel slots are
`s1.headline`, `s2.body` and so on. An empty `slots` map is a template preview: the template's
specimen copy is used and the run records `preview: true`.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from thallo.brand.templates import MAX_SLIDES, Template, TemplateBook
from thallo.brand.tokens import TEMPLATES_DIR, Brand, ScriptTokens, TokenError
from thallo.contracts import connection as c
from thallo.contracts.enums import Channel, Language

META_KEYS = ("_lang", "_channel", "_output", "_slide")
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".svg")
SPECIMEN_PHOTO = TEMPLATES_DIR / "specimen" / "abstract.svg"
DARK_BACKGROUNDS = ("bg-ink", "bg-accent", "bg-photo")
LANDSCAPE_REF_HEIGHT = 756  # 0.7 of 1080: below this height, type scales with height
_CAROUSEL_KEY = re.compile(r"^s(?P<n>[1-9]\d?)\.(?P<slot>[a-z_]+)$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
_LAYOUT_MARK = "thallo-layout"
_FONTS_MARK = "thallo-fonts"

Output = Literal["png", "pdf"]


class PlanError(ValueError):
    """The request cannot be turned into a render at all (unknown template, bad reserved slot,
    photo outside the allowed folders). The provider reports it as a FAIL, never a FLAG."""


class RendererUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SlideSpec:
    number: int  # 1-based position in the post (carousel) or 1
    slots: Mapping[str, str]
    photo: Path | None = None
    first: bool = True
    last: bool = True


@dataclass(frozen=True)
class Job:
    brand: Brand
    book: TemplateBook
    template: Template
    language: Language
    channel: Channel | None
    width: int
    height: int
    output: Output
    slides: tuple[SlideSpec, ...]
    preview: bool = False
    raw_slots: Mapping[str, str] = field(default_factory=dict)

    @property
    def script(self) -> ScriptTokens:
        return self.brand.for_language(self.language)

    @property
    def scale(self) -> float:
        """Width relative to the 1080 px reference. Channel minimums are stated against it."""
        return self.width / 1080

    @property
    def type_scale(self) -> float:
        """What tokens are multiplied by: width-led, but a short landscape canvas scales by height
        so the copy still fits. The lint then holds the result to the channel's minimum size."""
        return min(self.width / 1080, self.height / LANDSCAPE_REF_HEIGHT)


# ====================================================================== planning

def _photo(value: str, roots: Sequence[Path]) -> Path:
    p = Path(value).expanduser().resolve()
    if p.suffix.lower() not in PHOTO_SUFFIXES:
        raise PlanError(f"photo {p.name!r} is not an image this renderer takes")
    if not any(p.is_relative_to(r.resolve()) for r in roots):
        raise PlanError(f"photo {p.name!r} is outside the allowed photo folders")
    if not p.is_file():
        raise PlanError(f"photo {p.name!r} does not exist")
    return p


def _carousel_slides(slots: Mapping[str, str], roots: Sequence[Path], output: str,
                     slide_pick: str | None) -> list[SlideSpec]:
    per: dict[int, dict[str, str]] = {}
    for k, v in slots.items():
        m = _CAROUSEL_KEY.match(k)
        if not m:
            raise PlanError(f"carousel slot {k!r} must look like s1.headline")
        per.setdefault(int(m["n"]), {})[m["slot"]] = v
    numbers = sorted(per)
    if numbers != list(range(1, len(numbers) + 1)):
        raise PlanError(f"carousel slides must be numbered from 1 with no gap, got {numbers}")
    if len(numbers) > MAX_SLIDES:
        raise PlanError(f"a carousel has at most {MAX_SLIDES} slides")
    last = len(numbers)
    specs = [SlideSpec(n, {k: v for k, v in per[n].items() if k != "photo"},
                       _photo(per[n]["photo"], roots) if per[n].get("photo") else None,
                       first=n == 1, last=n == last) for n in numbers]
    if slide_pick is not None:
        if output == "pdf":
            raise PlanError("_slide picks one PNG slide; a PDF holds every slide")
        if not slide_pick.isdigit() or not 1 <= int(slide_pick) <= last:
            raise PlanError(f"_slide must be a slide number from 1 to {last}")
        return [s for s in specs if s.number == int(slide_pick)]
    if output == "png" and last > 1:
        raise PlanError("a carousel PNG needs _slide; use _output pdf for every slide")
    return specs


def plan_job(request: c.ImageRequest, *, brand: Brand, book: TemplateBook, default_language: Language = Language.EN,
             photo_roots: Sequence[Path] = (), default_output: Output = "png") -> Job:
    if request.purpose != "template_render" or not request.template_ref:
        raise PlanError("this renderer only takes purpose template_render with a template_ref")
    try:
        template = book.get(request.template_ref)
    except TokenError as exc:
        raise PlanError(str(exc)) from None
    slots = dict(request.slots)
    bad_meta = sorted(k for k in slots if k.startswith("_") and k not in META_KEYS)
    if bad_meta:
        raise PlanError(f"unknown reserved slots {bad_meta}")
    try:
        language = Language(slots.pop("_lang", default_language.value))
        channel = Channel(slots.pop("_channel")) if "_channel" in slots else None
    except ValueError as exc:
        raise PlanError(str(exc)) from None
    output = slots.pop("_output", default_output)
    if output not in ("png", "pdf"):
        raise PlanError("_output must be png or pdf")
    slide_pick = slots.pop("_slide", None)
    try:
        brand.for_language(language)
    except TokenError as exc:
        raise PlanError(str(exc)) from None

    preview = not slots
    roots = list(photo_roots)
    if preview:
        slots = dict(template.family.specimen[language])
        roots.append(SPECIMEN_PHOTO.parent)
        if template.layout.photo != "none":
            if template.family.carousel:
                n = max(int(m["n"]) for k in slots if (m := _CAROUSEL_KEY.match(k)))
                slots.update({f"s{i}.photo": str(SPECIMEN_PHOTO) for i in range(1, n + 1)})
            else:
                slots["photo"] = str(SPECIMEN_PHOTO)

    if template.family.carousel:
        specs = _carousel_slides(slots, roots, output, slide_pick)
    else:
        if slide_pick is not None:
            raise PlanError("_slide is only for carousels")
        if any(_CAROUSEL_KEY.match(k) for k in slots):
            raise PlanError("slide-numbered slots are only for carousels")
        photo = _photo(slots["photo"], roots) if slots.get("photo") else None
        specs = [SlideSpec(1, {k: v for k, v in slots.items() if k != "photo"}, photo)]
    return Job(brand=brand, book=book, template=template, language=language, channel=channel,
               width=request.width, height=request.height, output=output, slides=tuple(specs),
               preview=preview, raw_slots=dict(request.slots))


# ====================================================================== composing

def font_alias(script: str, role: str) -> str:
    return f"thallo-{script}-{role}"


def _font_faces(brand: Brand) -> str:
    out = []
    for script, st in sorted(brand.scripts.items()):
        for role, face in sorted(st.fonts.items()):
            srcs = [f'local("{n}")' for n in face.local]
            srcs += [f'url("{brand.file(rel).as_uri()}")' for rel in face.files]
            out.append(f'@font-face {{ font-family: "{font_alias(script, role)}"; src: {", ".join(srcs)}; '
                       f"font-weight: {face.weight}; }}")
    return "\n".join(out)


def _root_vars(brand: Brand) -> str:
    t = brand.tokens
    pairs = [f"--c-{k}: {v}" for k, v in t.colors.items()]
    pairs.append(f"--c-scrim: {t.colors[t.photo_scrim]}")
    pairs += [f"--fs-{k}: {v:g}" for k, v in t.type_scale.items()]
    pairs += [f"--sp-{k}: {v:g}" for k, v in t.spacing.items()]
    pairs += [f"--r-{k}: {v:g}" for k, v in t.radii.items()]
    pairs.append(f"--r-card: {t.radii.get('card', 0):g}")
    return ":root { " + "; ".join(pairs) + "; }"


def canvas_shape(width: int, height: int) -> str:
    r = height / width
    if r >= 1.5:
        return "tall"
    if r >= 1.1:
        return "portrait"
    if r >= 0.9:
        return "square"
    return "landscape"


def safe_insets(job: Job) -> tuple[float, float, float, float]:
    _, safe, _ = job.book.channels.rule(job.channel, job.width, job.height)
    return safe[0] * job.height, safe[1] * job.width, safe[2] * job.height, safe[3] * job.width


def _slide_html(job: Job, slide: SlideSpec, index: int) -> str:
    st = job.script
    tokens = job.brand.tokens
    s = job.type_scale
    logo_w = tokens.logo.width * s
    logo_h = logo_w / tokens.logo.aspect
    top, right, bottom, left = safe_insets(job)
    display_weight = st.fonts["display"].weight
    style = (f"--w: {job.width}; --h: {job.height}; --s: {s:.6f}; --sf: {st.size_factor:g}; --lh: {st.line_height:g}; "
             f'--font-display: "{font_alias(st.script, "display")}"; --font-body: "{font_alias(st.script, "body")}"; '
             f"--fw-display: {display_weight}; "
             f"--safe-t: {top:.2f}; --safe-r: {right:.2f}; --safe-b: {bottom:.2f}; --safe-l: {left:.2f}; "
             f"--logo-w: {logo_w:.2f}; --logo-h: {logo_h:.2f}; --logo-clear: {tokens.logo.clear_space * logo_h:.2f}")
    classes = ["slide", f"fam-{job.template.family_name}", job.template.layout.classes, f"script-{st.script}",
               f"shape-{canvas_shape(job.width, job.height)}"]
    classes += ["slide-first"] if slide.first else []
    classes += ["slide-last"] if slide.last else []
    attrs = (f'class="{html.escape(" ".join(classes))}" lang="{job.language.value}" data-slide="{index}" '
             f'data-number="{slide.number}" data-script="{st.script}" style="{html.escape(style)}"')
    dark = any(b in job.template.layout.classes.split() for b in DARK_BACKGROUNDS)
    logo_file = tokens.logo.file_on_dark if dark and tokens.logo.file_on_dark else tokens.logo.file
    photo_style = (f'background-image: url("{slide.photo.as_uri()}")' if slide.photo else "")
    trusted = {"slide_attrs": attrs, "logo_src": html.escape(job.brand.file(logo_file).as_uri()),
               "photo_style": html.escape(photo_style)}

    def fill(m: re.Match[str]) -> str:
        name = m[1]
        if name in trusted:
            return trusted[name]
        return html.escape(slide.slots.get(name, "").strip()).replace("\n", "<br>")

    return _PLACEHOLDER.sub(fill, job.book.fragment(job.template))


MEASURE_JS = r"""
(async () => {
  const mode = location.hash;
  if (mode !== '#measure' && mode !== '#output') return;
  const fonts = {};
  for (const f of JSON.parse(document.body.dataset.fonts)) {
    try { const r = await document.fonts.load('16px "' + f + '"', 'a');
          fonts[f] = r.length > 0 && r.every(x => x.status === 'loaded'); }
    catch (e) { fonts[f] = false; }
  }
  await document.fonts.ready;
  const mark = (name, obj) => { const m = document.createElement('meta'); m.name = name;
    m.content = btoa(unescape(encodeURIComponent(JSON.stringify(obj)))); document.head.appendChild(m); };
  if (mode === '#output') {
    for (const f of document.fonts) { if (f.status !== 'loaded') fonts[f.family.replace(/^["']|["']$/g, '')] = false; }
    mark('__FONTS__', fonts);  // the page itself is left as it is: this run writes the bytes
    return;
  }
  const px = v => parseFloat(v) || 0;
  const slides = [];
  for (const s of document.querySelectorAll('.slide')) {
    s.scrollIntoView({block: 'start', inline: 'start'});
    const o = s.getBoundingClientRect();
    const rel = r => [r.left - o.left, r.top - o.top, r.width, r.height];
    const boxes = [];
    for (const el of s.querySelectorAll('[data-slot],[data-role]')) {
      const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
      const text = !!el.dataset.slot;
      const b = {name: el.dataset.slot || el.dataset.role, kind: text ? 'text' : el.dataset.role, rect: rel(r),
                 visible: cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0,
                 empty: text ? el.textContent.trim() === '' : false};
      if (text && b.visible) {
        b.font_px = px(cs.fontSize);
        b.family = cs.fontFamily.split(',')[0].trim().replace(/^["']|["']$/g, '');
        b.color = cs.color;
        b.overflow_x = el.scrollWidth > el.clientWidth + 1;
        b.overflow_y = el.scrollHeight > el.clientHeight + 1;
        b.stacks = [];
        for (const [fx, fy] of [[.5, .5], [.08, .25], [.92, .25], [.08, .75], [.92, .75]]) {
          const st = [];
          for (const n of document.elementsFromPoint(r.left + r.width * fx, r.top + r.height * fy)) {
            if (n !== s && !s.contains(n)) continue;
            const c = getComputedStyle(n);
            const img = (n.tagName === 'IMG' && n.dataset.role !== 'logo') || c.backgroundImage !== 'none';
            st.push({c: c.backgroundColor, img});
            if (n === s) break;
          }
          b.stacks.push(st);
        }
      }
      boxes.push(b);
    }
    const regions = [...s.querySelectorAll('[data-region]')].map(el => ({name: el.dataset.region,
      rect: rel(el.getBoundingClientRect()), overflow_x: el.scrollWidth > el.clientWidth + 1,
      overflow_y: el.scrollHeight > el.clientHeight + 1}));
    slides.push({index: +s.dataset.slide, number: +s.dataset.number, script: s.dataset.script,
                 rect: [0, 0, o.width, o.height], boxes, regions});
  }
  document.body.innerHTML = '';
  mark('__MARK__', {fonts, slides});
})();
""".replace("__MARK__", _LAYOUT_MARK).replace("__FONTS__", _FONTS_MARK)

CSP = ("default-src 'none'; img-src file: data:; style-src 'unsafe-inline'; font-src file: data:; "
       "script-src 'unsafe-inline'")


def compose(jobs: Sequence[Job], *, batch: bool = False) -> str:
    """One HTML document for one job, or for many jobs of the same brand (batch measurement)."""
    if not jobs:
        raise ValueError("nothing to compose")
    brand = jobs[0].brand
    if any(j.brand.root != brand.root for j in jobs):
        raise ValueError("one document holds one brand")
    book = jobs[0].book
    aliases = sorted({font_alias(s, r) for s, st in brand.scripts.items() for r in st.fonts})
    slides, i = [], 0
    for job in jobs:
        for slide in job.slides:
            slides.append(_slide_html(job, slide, i))
            i += 1
    page = ""
    if not batch:
        j = jobs[0]
        page = f"@page {{ size: {j.width}px {j.height}px; margin: 0; }} html, body {{ width: {j.width}px; }}"
    lang = jobs[0].language.value
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f"<style>\n{_font_faces(brand)}\n{_root_vars(brand)}\n{book.css()}\n{page}\n</style></head>"
            f'<body class="{"batch" if batch else "single"}" data-fonts="{html.escape(json.dumps(aliases))}">'
            + "\n".join(slides) + f"<script>{MEASURE_JS}</script></body></html>")


def batch_viewport(jobs: Sequence[Job]) -> tuple[int, int]:
    return max(j.width for j in jobs), max(j.height for j in jobs)


# ====================================================================== rendering

@dataclass(frozen=True)
class Rendered:
    """The bytes of one output run, and the fonts as that same run loaded them (alias -> loaded).
    `fonts` is None when the run did not report them; the lint treats that as a FAIL."""

    data: bytes
    fonts: dict[str, bool] | None


class Renderer(Protocol):
    name: str

    def version(self) -> str: ...
    def measure(self, document: str, width: int, height: int) -> dict: ...
    def png(self, document: str, width: int, height: int) -> Rendered: ...
    def pdf(self, document: str, width: int, height: int) -> Rendered: ...


def _candidates(environ: Mapping[str, str]) -> list[str]:
    home = Path(environ.get("HOME", str(Path.home())))
    caches = [environ["PLAYWRIGHT_BROWSERS_PATH"]] if environ.get("PLAYWRIGHT_BROWSERS_PATH") else []
    caches += [str(home / "Library" / "Caches" / "ms-playwright"), str(home / ".cache" / "ms-playwright")]
    found: list[str] = []
    for cache in caches:
        found += sorted(glob.glob(f"{cache}/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"),
                        reverse=True)
        found += sorted(glob.glob(f"{cache}/chromium_headless_shell-*/chrome-linux*/headless_shell"), reverse=True)
    found += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    found += [p for n in ("chromium", "chromium-browser", "google-chrome") if (p := shutil.which(n))]
    return found


def find_chromium(environ: Mapping[str, str] | None = None) -> Path | None:
    """THALLO_CHROMIUM first; then a Playwright headless shell already on disk; then a Chrome or
    Chromium install. Never downloads anything. None when there is no browser."""
    env = os.environ if environ is None else environ
    if env.get("THALLO_CHROMIUM"):
        p = Path(env["THALLO_CHROMIUM"])
        return p if p.is_file() and os.access(p, os.X_OK) else None
    for cand in _candidates(env):
        p = Path(cand)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


class ChromiumRenderer:
    name = "chromium"

    def __init__(self, binary: Path, timeout_s: float = 90.0) -> None:
        self.binary = Path(binary)
        self.timeout_s = timeout_s

    def version(self) -> str:
        for part in reversed(self.binary.parts):
            if part.startswith(("chromium_headless_shell-", "chromium-")):
                return part
        return self.binary.name

    def _run(self, document: str, width: int, height: int, action: list[str], fragment: str = "") -> tuple[str, Path]:
        work = Path(tempfile.mkdtemp(prefix="thallo-render-"))
        try:
            page = work / "page.html"
            page.write_text(document, encoding="utf-8")
            args = [str(self.binary), "--headless", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
                    "--force-device-scale-factor=1", "--no-first-run", "--no-default-browser-check",
                    "--disable-extensions", "--disable-background-networking", "--disable-sync",
                    "--disable-component-update", "--disable-default-apps", "--metrics-recording-only",
                    "--host-resolver-rules=MAP * ~NOTFOUND", f"--user-data-dir={work / 'profile'}",
                    "--virtual-time-budget=4000", f"--window-size={width},{height}", *action,
                    page.as_uri() + fragment]
            done = subprocess.run(args, capture_output=True, timeout=self.timeout_s, check=False,  # noqa: S603
                                  cwd=work, text=True)
            if done.returncode != 0:
                raise RendererUnavailable(f"chromium exited with {done.returncode}")
            return done.stdout, work
        except subprocess.TimeoutExpired:
            shutil.rmtree(work, ignore_errors=True)
            raise RendererUnavailable("chromium timed out") from None
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise

    def measure(self, document: str, width: int, height: int) -> dict:
        out, work = self._run(document, width, height, ["--dump-dom"], "#measure")
        shutil.rmtree(work, ignore_errors=True)
        data = _marked(out, _LAYOUT_MARK)
        if data is None:
            raise RendererUnavailable("chromium returned no layout")
        return data

    def _file(self, document: str, width: int, height: int, action: list[str], name: str) -> Rendered:
        """One Chromium run writes the file and dumps the DOM, so the font report comes from the
        very run whose bytes are shipped, not from the measuring run."""
        out, work = self._run(document, width, height, ["--dump-dom", *(a.replace("{out}", name) for a in action)],
                              "#output")
        try:
            target = work / name
            if not target.is_file():
                raise RendererUnavailable(f"chromium wrote no {name}")
            fonts = _marked(out, _FONTS_MARK)
            ok = isinstance(fonts, dict) and all(isinstance(v, bool) for v in fonts.values())
            return Rendered(target.read_bytes(), fonts if ok else None)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def png(self, document: str, width: int, height: int) -> Rendered:
        return self._file(document, width, height, ["--screenshot={out}"], "out.png")

    def pdf(self, document: str, width: int, height: int) -> Rendered:
        return self._file(document, width, height, ["--print-to-pdf={out}", "--no-pdf-header-footer"], "out.pdf")


def _marked(dom: str, name: str) -> dict | None:
    m = re.search(rf'<meta name="{name}" content="([A-Za-z0-9+/=]+)"', dom)
    if not m:
        return None
    try:
        data = json.loads(base64.b64decode(m[1]).decode("utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------- PDF determinism

PDF_EPOCH_DIGITS = b"20000101000000"  # fixed, never the wall clock or the injected clock
_PDF_DATE = re.compile(rb"(/(?:CreationDate|ModDate)\s*\(D:)(\d{4,14})([^)]*)(\))")
_XMP_EPOCH = b"2000-01-01T00:00:00.000000"
_XMP_PACKET = re.compile(rb"<x:xmpmeta.*?</x:xmpmeta>", re.S)
_XMP_DATE = re.compile(rb"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)?")
_XMP_UUID = re.compile(rb"(uuid:)([0-9a-fA-F-]{8,})")
_PDF_ID = re.compile(rb"(/ID\s*\[\s*<)([0-9a-fA-F]*)(>\s*<)([0-9a-fA-F]*)(>\s*\])")


class PdfNotDeterministic(RendererUnavailable):
    """The PDF holds metadata this code cannot rewrite in place (for example a compressed XMP
    stream). Refused rather than shipped with a time-dependent hash."""


def _same_len(value: bytes, fill: bytes) -> bytes:
    return (fill * (len(value) // len(fill) + 1))[:len(value)]


def _zero_tz(tz: bytes) -> bytes:
    return re.sub(rb"\d", b"0", tz).replace(b"-", b"+")


def deterministic_pdf(data: bytes) -> bytes:
    """Make PDF bytes independent of when they were written. Every rewrite keeps the byte
    length, so the cross-reference offsets stay valid.

    - `/CreationDate` and `/ModDate`: set to a fixed epoch (2000-01-01 00:00 UTC).
    - XMP metadata, when stored as plain text: dates to the same epoch, uuids to a digest.
    - The trailer `/ID`: a digest of the normalised bytes, so it depends on content only."""
    if not data.startswith(b"%PDF"):
        return data

    def date(m: re.Match[bytes]) -> bytes:
        return m[1] + PDF_EPOCH_DIGITS[:len(m[2])] + _zero_tz(m[3]) + m[4]

    out = _PDF_DATE.sub(date, data)

    def xmp_date(d: re.Match[bytes]) -> bytes:
        return bytes(t if chr(ch).isdigit() and chr(t).isdigit() else (48 if chr(ch).isdigit() else ch)
                     for ch, t in zip(d[0], _XMP_EPOCH.ljust(len(d[0]), b"0")[:len(d[0])], strict=True))

    def xmp(m: re.Match[bytes]) -> bytes:
        packet = _XMP_DATE.sub(xmp_date, m[0])
        return _XMP_UUID.sub(lambda u: u[1] + re.sub(rb"[0-9a-fA-F]", b"0", u[2]), packet)

    out = _XMP_PACKET.sub(xmp, out)
    if b"/Metadata" in out and not _XMP_PACKET.search(out):
        raise PdfNotDeterministic("the PDF carries XMP metadata this renderer cannot normalise")
    blank = _PDF_ID.sub(lambda m: m[1] + b"0" * len(m[2]) + m[3] + b"0" * len(m[4]) + m[5], out)
    if blank != out:
        digest = hashlib.sha256(blank).hexdigest().encode()
        out = _PDF_ID.sub(lambda m: m[1] + _same_len(m[2], digest) + m[3] + _same_len(m[4], digest) + m[5], blank)
    if len(out) != len(data):  # pragma: no cover - every substitution above keeps length
        raise PdfNotDeterministic("PDF normalisation changed the byte length")
    return out


def render_bytes(renderer: Renderer, job: Job, document: str) -> Rendered:
    """The output run. A PDF is normalised so its bytes, and so its sha256, do not depend on
    the wall clock: a later re-render of an approved request gives the same hash."""
    if job.output == "pdf":
        r = renderer.pdf(document, job.width, job.height)
        return Rendered(deterministic_pdf(r.data), r.fonts)
    return renderer.png(document, job.width, job.height)
