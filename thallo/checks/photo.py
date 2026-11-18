"""Technical checks on a client photo (quality bar 3): resolution, blur, exposure, crop inside the
safe zone. Code, no model. Layer CODE, every row keyed on the photo's bytes hash.

The measurements are pure functions over a decoded `Raster` (grey levels). Decoding is separate:
`decode` reads PNG with the standard library only. Other formats (JPEG, HEIC, WebP) need an image
library that is not a dependency yet; for them the photo check FAILs with that reason named, since
a row that measured nothing must never cover the photo.

Verdicts: too small for the canvas, or a crop that cuts the marked subject or pushes it out of
the safe zone, FAIL. Blur and exposure are estimates, so they FLAG for a person to look. The
summary row `media.photo` carries the worst verdict.
"""
from __future__ import annotations

import struct
import zlib
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
    grey: bytes  # one byte per pixel, row by row

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

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> Raster:
    """8-bit, non-interlaced grey, grey-alpha, RGB or RGBA PNG to grey levels (Rec. 709 luma)."""
    if not data.startswith(_PNG_SIG):
        raise PhotoUndecodable("not a PNG")
    pos, idat, header = 8, bytearray(), None
    while pos + 8 <= len(data):
        (size,) = struct.unpack(">I", data[pos:pos + 4])
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + size]
        pos += 12 + size
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise PhotoUndecodable("PNG without a header")
    width, height, depth, colour, _, _, interlace = header
    if depth != 8 or colour not in _CHANNELS or interlace:
        raise PhotoUndecodable(f"PNG colour type {colour}, depth {depth}, interlace {interlace} is not read here")
    channels = _CHANNELS[colour]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PhotoUndecodable(f"PNG data is corrupt: {exc}") from None
    if len(raw) < height * (stride + 1):
        raise PhotoUndecodable("PNG data is shorter than its size")
    prev = bytearray(stride)
    grey = bytearray(width * height)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = prev[i]
            corner = prev[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                line[i] = (line[i] + _paeth(left, up, corner)) & 0xFF
            elif kind != 0:
                raise PhotoUndecodable(f"PNG filter {kind} is unknown")
        for x in range(width):
            px = line[x * channels:(x + 1) * channels]
            if channels >= 3:
                grey[y * width + x] = min(255, round(0.2126 * px[0] + 0.7152 * px[1] + 0.0722 * px[2]))
            else:
                grey[y * width + x] = px[0]
        prev = line
    return Raster(width, height, bytes(grey))


def decode(data: bytes, mime: str) -> Raster:
    if mime.lower() == "image/png":
        return decode_png(data)
    raise PhotoUndecodable(f"no decoder for {mime}: an image library must be added before this can be measured")


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
    scale = max(target.width / r.width, target.height / r.height)
    if scale > 1.0:
        out.append(f(RESOLUTION, CheckVerdict.FAIL, f"{r.width}x{r.height} must be enlarged {scale:.2f} times to "
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
    box = crop_box(r.width, r.height, target)
    loss = 1 - ((box.right - box.left) * (box.bottom - box.top)) / (r.width * r.height)
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
    "blur_variance", "check_photo", "crop_box", "decode", "decode_png", "exposure", "undecodable", "worst",
]
