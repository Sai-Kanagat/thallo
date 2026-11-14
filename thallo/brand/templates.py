"""Templates, channel sizes and the rotation rule (BUILD_PLAN 2.7).

`brand_templates/manifest.json` holds six format families. Each family has one HTML slide
fragment, slot limits, specimen copy for previews, and at least eight layouts; template ids are
`<family>-01` to `<family>-08`. `brand_templates/channels.json` holds every channel size with
its minimum font size and safe zone.

Rotation: the same template never appears twice in any five consecutive posts.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from thallo.brand.tokens import TEMPLATES_DIR, TokenError
from thallo.contracts.enums import Channel, Language

ROTATION_WINDOW = 5
MIN_TEMPLATES_PER_FAMILY = 8
MAX_SLIDES = 10
_REF = re.compile(r"^(?P<family>[a-z_]+)-(?P<n>\d{2})$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlotSpec(_Strict):
    role: Literal["text", "photo"] = "text"
    max_chars: int | None = Field(default=None, gt=0)
    max_words: int | None = Field(default=None, gt=0)
    required: bool = False


class Layout(_Strict):
    classes: str = Field(pattern=r"^[a-z0-9 -]+$")
    photo: Literal["none", "full", "split"] = "none"
    note: str = ""


class Family(_Strict):
    fragment: str
    slots: dict[str, SlotSpec]
    max_words_per_slide: int = Field(gt=0)
    carousel: bool = False
    min_slides: int = 1
    specimen: dict[Language, dict[str, str]]
    layouts: tuple[Layout, ...]

    @model_validator(mode="after")
    def _check(self) -> Family:
        if len(self.layouts) < MIN_TEMPLATES_PER_FAMILY:
            raise ValueError(f"a family needs at least {MIN_TEMPLATES_PER_FAMILY} layouts")
        if len({lay.classes for lay in self.layouts}) != len(self.layouts):
            raise ValueError("two layouts in one family are identical")
        if any(lay.photo != "none" for lay in self.layouts) and "photo" not in self.slots:
            raise ValueError("a photo layout needs a photo slot")
        if set(self.specimen) != set(Language):
            raise ValueError("specimen copy is needed in every language")
        return self


class Template(_Strict):
    ref: str
    family_name: str
    family: Family
    layout: Layout
    index: int

    def slot_spec(self, name: str) -> SlotSpec:
        spec = self.family.slots[name]
        if name == "photo" and self.layout.photo != "none":
            return spec.model_copy(update={"required": True})
        return spec


class SizeRule(_Strict):
    w: int = Field(gt=0)
    h: int = Field(gt=0)
    safe: tuple[float, float, float, float] | None = None  # top, right, bottom, left as a share of the side
    use: str = ""


class ChannelRule(_Strict):
    min_font_px: float = Field(gt=0, description="At 1080 px wide")
    safe: tuple[float, float, float, float]
    sizes: tuple[SizeRule, ...] = ()


class ChannelBook(_Strict):
    note: str = ""
    default: ChannelRule
    channels: dict[Channel, ChannelRule]

    def rule(self, channel: Channel | None, width: int, height: int) -> tuple[float, tuple[float, ...], bool]:
        """(min_font_px at 1080 wide, safe insets, size_is_listed). With no channel, the strictest
        minimum font of any channel and the default safe zone apply."""
        if channel is None:
            strict = max([self.default.min_font_px, *(r.min_font_px for r in self.channels.values())])
            return strict, self.default.safe, True
        r = self.channels.get(channel)
        if r is None:
            return self.default.min_font_px, self.default.safe, False
        size = next((s for s in r.sizes if (s.w, s.h) == (width, height)), None)
        return r.min_font_px, (size.safe if size and size.safe else r.safe), size is not None

    def all_sizes(self) -> list[tuple[int, int]]:
        return sorted({(s.w, s.h) for r in self.channels.values() for s in r.sizes})


class TemplateBook(_Strict):
    root: Path
    families: dict[str, Family]
    channels: ChannelBook

    def get(self, ref: str) -> Template:
        m = _REF.match(ref)
        if not m or m["family"] not in self.families:
            raise TokenError(f"unknown template {ref!r}")
        fam = self.families[m["family"]]
        n = int(m["n"])
        if not 1 <= n <= len(fam.layouts):
            raise TokenError(f"unknown template {ref!r}")
        return Template(ref=ref, family_name=m["family"], family=fam, layout=fam.layouts[n - 1], index=n)

    def refs(self, family: str | None = None) -> list[str]:
        return [f"{name}-{i:02d}" for name, fam in self.families.items() if family in (None, name)
                for i in range(1, len(fam.layouts) + 1)]

    def fragment(self, template: Template) -> str:
        p = (self.root / template.family.fragment).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise TokenError("fragment outside the templates directory")
        return p.read_text(encoding="utf-8")

    def css(self) -> str:
        return (self.root / "base.css").read_text(encoding="utf-8")


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenError(f"cannot read {path.name}: {exc}") from None


def load_templates(root: str | Path = TEMPLATES_DIR) -> TemplateBook:
    root = Path(root).resolve()
    try:
        man = _read(root / "manifest.json")
        fams = {k: Family.model_validate(v) for k, v in man["families"].items()}
        ch = ChannelBook.model_validate(_read(root / "channels.json"))
    except (ValueError, KeyError) as exc:
        raise TokenError(f"bad template manifest: {exc}") from None
    book = TemplateBook(root=root, families=fams, channels=ch)
    for name, fam in fams.items():
        frag = book.fragment(book.get(f"{name}-01"))
        used = set(_PLACEHOLDER.findall(frag)) - {"logo_src", "photo_style", "slide_attrs"}
        if used != set(fam.slots) - {"photo"}:
            raise TokenError(f"{name}: fragment slots {sorted(used)} do not match the manifest")
    return book


@cache
def default_templates() -> TemplateBook:
    return load_templates(TEMPLATES_DIR)


# ---------------------------------------------------------------------- rotation

def rotation_ok(history: Sequence[str], window: int = ROTATION_WINDOW) -> bool:
    """True when no template appears twice in any `window` consecutive posts (oldest first)."""
    return all(len(set(history[i:i + window])) == len(history[i:i + window])
               for i in range(max(1, len(history) - window + 1)))


def choose_template(book: TemplateBook, family: str, recent: Sequence[str], seed: str,
                    window: int = ROTATION_WINDOW) -> str:
    """Pick a template of `family` that keeps the rotation rule, given `recent` refs oldest first.
    Deterministic for the same seed, so a re-run picks the same template."""
    blocked = set(recent[-(window - 1):]) if window > 1 else set()
    options = [r for r in book.refs(family) if r not in blocked]
    if not options:
        raise TokenError(f"family {family} has too few templates for a rotation window of {window}")
    pick = int(hashlib.sha256(f"{family}:{seed}".encode()).hexdigest(), 16) % len(options)
    return options[pick]
