"""Technical checks on a client photo (quality bar 3): resolution, blur, exposure, crop inside the
safe zone. Code, no model. Layer CODE, every row keyed on the photo's bytes hash.

The measurements are pure functions over a decoded `Raster` (grey levels). Decoding is separate:
`decode` reads JPEG, PNG and WebP with Pillow under explicit size limits. Anything it cannot read
FAILs the photo check with the reason named, since a row that measured nothing must never cover
the photo.

Verdicts: too small for the canvas, or a crop that cuts the marked subject or pushes it out of
the safe zone, FAIL. Blur and exposure are estimates, so they FLAG for a person to look. The
summary row `media.photo` carries the worst verdict.
"""
from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass

from thallo.checks.text import Finding
from thallo.contracts.enums import CheckVerdict
from thallo.gate.checks import PHOTO_ATTESTATION, PHOTO_CHECK

RESOLUTION = f"{PHOTO_CHECK}.resolution"
BLUR = f"{PHOTO_CHECK}.blur"
EXPOSURE = f"{PHOTO_CHECK}.exposure"
CROP = f"{PHOTO_CHECK}.crop"

BLUR_MIN_VARIANCE = 60.0  # variance of the Laplacian on grey levels, sampled to SAMPLE_SIDE
SAMPLE_SIDE = 256
DARK, BRIGHT = 30.0, 225.0  # mean grey level outside this band: under or over exposed
CLIPPED_SHARE = 0.25  # share of samples at the ends of the range
MAX_CROP_LOSS = 0.35  # crop to the canvas loses more than this share of the photo: FLAG, look at faces


class PhotoUndecodable(ValueError):
    pass


@dataclass(frozen=True)
class Raster:
    width: int
    height: int
    grey: bytes  # one byte per pixel, row by row (a sample, at most SAMPLE_SIDE a side)
    full: tuple[int, int] | None = None  # the image's own size in pixels

    @property
    def size(self) -> tuple[int, int]:
        return self.full or (self.width, self.height)

    def at(self, x: int, y: int) -> int:
        return self.grey[y * self.width + x]


@dataclass(frozen=True)
class Box:
    """A region in photo pixels: the subject a person marked (a face, the product)."""

    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True)
class Target:
    """The canvas the photo will fill, and its safe zone as insets in canvas pixels."""

    width: int
    height: int
    safe: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # top, right, bottom, left


# ---------------------------------------------------------------- decoding

MAX_SIDE = 16384
MAX_PIXELS = 40_000_000  # decoded as RGBA at most: 160 MB


def decode(data: bytes, mime: str) -> Raster:
    """JPEG, PNG or WebP to a grey `Raster` sampled to at most `SAMPLE_SIDE` a side, with the
    original size kept for the resolution and crop checks. Pillow reads it with an explicit pixel
    limit; the size is checked from the header before any pixel is decoded, and JPEGs are decoded
    at a reduced scale (`draft`), so work stays bounded. Any failure raises `PhotoUndecodable`."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a dependency
        raise PhotoUndecodable("no image library is installed") from None
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    if mime.lower() not in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
        raise PhotoUndecodable(f"no decoder for {mime}")
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE) or w * h > MAX_PIXELS:
                raise PhotoUndecodable(f"image size {w}x{h} is outside the limits")
            if im.format not in ("JPEG", "PNG", "WEBP"):
                raise PhotoUndecodable(f"{im.format} is not read here")
            im.draft("L", (SAMPLE_SIDE, SAMPLE_SIDE))
            grey = im.convert("L")
            grey.thumbnail((SAMPLE_SIDE, SAMPLE_SIDE))
            return Raster(grey.width, grey.height, grey.tobytes(), full=(w, h))
    except PhotoUndecodable:
        raise
    except Exception as exc:  # noqa: BLE001 - any decoder failure, bomb warnings included, is "not measured"
        raise PhotoUndecodable(f"image is malformed ({type(exc).__name__})") from None


# ---------------------------------------------------------------- measurements

def _sample(r: Raster, side: int = SAMPLE_SIDE) -> Raster:
    step = max(1, max(r.width, r.height) // side)
    w, h = max(1, r.width // step), max(1, r.height // step)
    return Raster(w, h, bytes(r.at(min(x * step, r.width - 1), min(y * step, r.height - 1))
                             for y in range(h) for x in range(w)))


def blur_variance(r: Raster) -> float:
    """Variance of the 4-neighbour Laplacian: low means few sharp edges."""
    s = _sample(r)
    if s.width < 3 or s.height < 3:
        return 0.0
    vals = [4 * s.at(x, y) - s.at(x - 1, y) - s.at(x + 1, y) - s.at(x, y - 1) - s.at(x, y + 1)
            for y in range(1, s.height - 1) for x in range(1, s.width - 1)]
    mean = sum(vals) / len(vals)
    return sum((v - mean) ** 2 for v in vals) / len(vals)


def exposure(r: Raster) -> tuple[float, float]:
    """(mean grey level, share of samples at 0 to 4 or 251 to 255)."""
    s = _sample(r)
    vals = s.grey
    clipped = sum(1 for v in vals if v <= 4 or v >= 251)
    return sum(vals) / len(vals), clipped / len(vals)


def crop_box(width: int, height: int, target: Target) -> Box:
    """The centred region of the photo that fills the target canvas."""
    want = target.width / target.height
    if width / height > want:
        w = height * want
        return Box((width - w) / 2, 0.0, (width + w) / 2, float(height))
    h = width / want
    return Box(0.0, (height - h) / 2, float(width), (height + h) / 2)


def check_photo(r: Raster, target: Target, *, subject: Box | None = None,
                media_sha256: str) -> list[Finding]:
    """One row per measure plus the `media.photo` summary, all keyed on `media_sha256`."""
    def f(check: str, verdict: CheckVerdict, reason: str | None = None) -> Finding:
        return Finding(check, verdict, reason, media_sha256)

    out: list[Finding] = []
    fw, fh = r.size
    scale = max(target.width / fw, target.height / fh)
    if scale > 1.0:
        out.append(f(RESOLUTION, CheckVerdict.FAIL, f"{fw}x{fh} must be enlarged {scale:.2f} times to "
                                                    f"fill {target.width}x{target.height}"))
    else:
        out.append(f(RESOLUTION, CheckVerdict.PASS))
    var = blur_variance(r)
    out.append(f(BLUR, CheckVerdict.FLAG, f"looks soft: edge variance {var:.0f}, below {BLUR_MIN_VARIANCE:.0f}")
               if var < BLUR_MIN_VARIANCE else f(BLUR, CheckVerdict.PASS))
    mean, clipped = exposure(r)
    problems = []
    if mean < DARK:
        problems.append(f"dark (mean level {mean:.0f})")
    if mean > BRIGHT:
        problems.append(f"bright (mean level {mean:.0f})")
    if clipped > CLIPPED_SHARE:
        problems.append(f"{clipped:.0%} of it is pure black or white")
    out.append(f(EXPOSURE, CheckVerdict.FLAG, "; ".join(problems)) if problems else f(EXPOSURE, CheckVerdict.PASS))
    box = crop_box(fw, fh, target)
    loss = 1 - ((box.right - box.left) * (box.bottom - box.top)) / (fw * fh)
    if subject is not None:
        k = (box.right - box.left) / target.width  # photo pixels per canvas pixel
        top, right, bottom, left = target.safe
        safe = Box(box.left + left * k, box.top + top * k, box.right - right * k, box.bottom - bottom * k)
        if not (subject.left >= box.left and subject.right <= box.right
                and subject.top >= box.top and subject.bottom <= box.bottom):
            out.append(f(CROP, CheckVerdict.FAIL, "the crop to the canvas cuts the marked subject"))
        elif not (subject.left >= safe.left and subject.right <= safe.right
                  and subject.top >= safe.top and subject.bottom <= safe.bottom):
            out.append(f(CROP, CheckVerdict.FAIL, "the marked subject sits outside the safe zone"))
        else:
            out.append(f(CROP, CheckVerdict.PASS))
    elif loss > MAX_CROP_LOSS:
        out.append(f(CROP, CheckVerdict.FLAG, f"the crop to the canvas drops {loss:.0%} of the photo and no subject "
                                              "is marked: check that no face or product is cut"))
    else:
        out.append(f(CROP, CheckVerdict.PASS))
    out.append(f(PHOTO_CHECK, worst(x.verdict for x in out),
                 "; ".join(f"{x.check}: {x.reason}" for x in out if x.reason) or None))
    return out


def undecodable(reason: str, *, media_sha256: str) -> list[Finding]:
    """The photo could not be measured: FAIL, never a covering PASS or FLAG."""
    return [Finding(PHOTO_CHECK, CheckVerdict.FAIL, f"not measured: {reason}", media_sha256)]


def attestation(*, media_sha256: str, provenance_note: str = "") -> Finding:
    """The human sign-off, recorded as a FLAG the approver must acknowledge at approval."""
    note = f" ({provenance_note})" if provenance_note else ""
    return Finding(PHOTO_ATTESTATION, CheckVerdict.FLAG,
                   "confirm this is the client's own photo, may be used, and shows no one without consent" + note,
                   media_sha256)


def worst(verdicts: Sequence[CheckVerdict] | object) -> CheckVerdict:
    vs = list(verdicts)  # type: ignore[call-overload]
    if CheckVerdict.FAIL in vs:
        return CheckVerdict.FAIL
    return CheckVerdict.FLAG if CheckVerdict.FLAG in vs else CheckVerdict.PASS


__all__ = [
    "BLUR", "CROP", "EXPOSURE", "RESOLUTION", "Box", "PhotoUndecodable", "Raster", "Target", "attestation",
    "blur_variance", "check_photo", "crop_box", "decode", "exposure", "undecodable", "worst",
]
