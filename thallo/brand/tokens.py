"""Brand design tokens (BUILD_PLAN 2.7, module 4).

A brand lives in one directory, outside this repository for real clients:

    <brand_dir>/brand.json          colours, type scale, spacing, radii, logo rules, photo scrim
    <brand_dir>/script/latin.json   fonts and setting for en and it
    <brand_dir>/script/tamil.json   fonts and setting for ta
    <brand_dir>/<logo file>         the logo the brand supplied

Type sizes and spacing are pixels at a 1080 px wide canvas; the renderer scales them by width.
The only brand in the repository is the fictional demo under `brand_templates/demo`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from thallo.contracts.enums import Language

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "brand_templates"
DEMO_BRAND_DIR = TEMPLATES_DIR / "demo"
BRAND_FILE = "brand.json"
DEMO_BRAND_FILE = "demo.brand.json"

Script = Literal["latin", "tamil"]
SCRIPT_FOR_LANGUAGE: dict[Language, Script] = {Language.EN: "latin", Language.IT: "latin", Language.TA: "tamil"}

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_RGBA = re.compile(r"^rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*(0|1|0?\.\d+)\s*\)$")
_FONT_NAME = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")


class TokenError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def script_for(language: Language) -> Script:
    return SCRIPT_FOR_LANGUAGE[language]


class Logo(_Strict):
    file: str
    file_on_dark: str | None = Field(default=None, description="Version for ink, accent and photo backgrounds")
    aspect: float = Field(gt=0, description="Width divided by height")
    width: float = Field(gt=0, description="Rendered width at 1080 px")
    min_width: float = Field(gt=0, description="Smallest allowed rendered width at 1080 px")
    clear_space: float = Field(ge=0, le=2, description="Free space around the logo, as a share of its height")


class Contrast(_Strict):
    normal: float = Field(default=4.5, ge=1)
    large: float = Field(default=3.0, ge=1)
    large_text_px: float = Field(default=64, gt=0, description="At 1080 px wide; at or above this is large text")


class BrandTokens(_Strict):
    brand: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    name: str
    fictional: bool = False
    colors: dict[str, str]
    type_scale: dict[str, float]
    spacing: dict[str, float]
    radii: dict[str, float] = Field(default_factory=dict)
    logo: Logo
    photo_scrim: str = Field(description="Colour token laid over photos under text")
    contrast: Contrast = Contrast()

    @field_validator("colors")
    @classmethod
    def _colours(cls, v: dict[str, str]) -> dict[str, str]:
        for k, col in v.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", k) or not (_HEX.match(col) or _RGBA.match(col)):
                raise ValueError(f"colour {k}={col!r}: use #rrggbb or rgba(r, g, b, a)")
        return v

    @field_validator("type_scale", "spacing", "radii")
    @classmethod
    def _numbers(cls, v: dict[str, float]) -> dict[str, float]:
        for k, n in v.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", k) or n < 0:
                raise ValueError(f"token {k}={n!r}: lower-case name, non-negative number")
        return v

    @model_validator(mode="after")
    def _refs(self) -> BrandTokens:
        missing = {"ink", "paper", "accent", "on_accent", "on_scrim"} - set(self.colors)
        if missing:
            raise ValueError(f"colours missing: {sorted(missing)}")
        if self.photo_scrim not in self.colors:
            raise ValueError("photo_scrim must name a colour token")
        need = {"display", "h1", "h2", "body", "small"} - set(self.type_scale)
        if need:
            raise ValueError(f"type scale missing: {sorted(need)}")
        if {"margin", "gap"} - set(self.spacing):
            raise ValueError("spacing needs margin and gap")
        if self.logo.width < self.logo.min_width:
            raise ValueError("logo width is below its own minimum")
        return self


class FontFace(_Strict):
    family: str = Field(description="Display name, for people")
    local: tuple[str, ...] = Field(default=(), description="Installed font names to try, in order")
    files: tuple[str, ...] = Field(default=(), description="Font files inside the brand directory")
    weight: int = 400

    @model_validator(mode="after")
    def _some_source(self) -> FontFace:
        if not self.local and not self.files:
            raise ValueError(f"font {self.family}: give local names or files")
        for n in self.local:
            if not _FONT_NAME.match(n):
                raise ValueError(f"font name {n!r} has characters a CSS local() cannot take")
        return self


class ScriptTokens(_Strict):
    script: Script
    languages: tuple[Language, ...]
    fonts: dict[Literal["display", "body"], FontFace]
    line_height: float = Field(gt=0.8, le=2.5)
    size_factor: float = Field(default=1.0, gt=0.5, le=1.5, description="Multiplies the type scale")
    char_factor: float = Field(default=1.0, gt=0.5, le=3, description="Multiplies slot character limits")

    @model_validator(mode="after")
    def _matches(self) -> ScriptTokens:
        if {"display", "body"} - set(self.fonts):
            raise ValueError("fonts need display and body")
        wrong = [lang for lang in self.languages if SCRIPT_FOR_LANGUAGE[lang] != self.script]
        if wrong:
            raise ValueError(f"{self.script} tokens cannot serve {wrong}")
        return self


class Brand(_Strict):
    """A loaded brand: its directory, tokens and the script tokens it has."""

    root: Path
    tokens: BrandTokens
    scripts: dict[str, ScriptTokens]

    def for_language(self, language: Language) -> ScriptTokens:
        s = script_for(language)
        if s not in self.scripts:
            raise TokenError(f"brand {self.tokens.brand} has no {s} tokens for {language.value}")
        st = self.scripts[s]
        if language not in st.languages:
            raise TokenError(f"brand {self.tokens.brand} {s} tokens do not list {language.value}")
        return st

    def file(self, rel: str) -> Path:
        """A file inside the brand directory. Refuses anything that resolves outside it."""
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root.resolve()) or not p.is_file():
            raise TokenError(f"brand file {rel!r} is missing or outside the brand directory")
        return p


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TokenError(f"cannot read {path.name}: {exc}") from None


def load_brand(brand_dir: str | Path) -> Brand:
    """Reads `brand.json` (git-ignored in this repo, so a client's tokens never get committed).
    The repo's own fixture is `demo.brand.json`, accepted only when it says `fictional: true`."""
    root = Path(brand_dir).resolve()
    try:
        if (root / BRAND_FILE).is_file():
            tokens = BrandTokens.model_validate(_read(root / BRAND_FILE))
        else:
            tokens = BrandTokens.model_validate(_read(root / DEMO_BRAND_FILE))
            if not tokens.fictional:
                raise TokenError(f"{DEMO_BRAND_FILE} is only for fictional demo brands")
        scripts: dict[str, ScriptTokens] = {}
        for f in sorted((root / "script").glob("*.json")):
            st = ScriptTokens.model_validate(_read(f))
            if f.stem != st.script:
                raise TokenError(f"{f.name} declares script {st.script}")
            scripts[st.script] = st
    except ValueError as exc:
        raise TokenError(str(exc)) from None
    brand = Brand(root=root, tokens=tokens, scripts=scripts)
    brand.file(tokens.logo.file)
    if tokens.logo.file_on_dark:
        brand.file(tokens.logo.file_on_dark)
    for st in scripts.values():
        for face in st.fonts.values():
            for rel in face.files:
                brand.file(rel)
    return brand
