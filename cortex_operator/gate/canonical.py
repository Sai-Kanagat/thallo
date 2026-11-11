"""The canonical form and the bundle root hash. Written down in CANONICAL_FORM.md; keep both in step.

A bundle root hash covers, for every variant: its canonical text, every media file's bytes
(sha256 of the content, never the path), every used claim with its evidence hash, its
links resolved to their destinations, its channel and language, plus the bundle's rule
version, tenant and id. Any change to any of these changes the root, which voids the
approval bound to it.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cortex_operator.contracts.models import Bundle, Claim, Media, Variant

CANONICAL_VERSION = 1

ROOT_DOMAIN = b"thallo/bundle-root/v1\n"
VARIANT_DOMAIN = b"thallo/variant/v1\n"

LinkResolver = Callable[[str], "str | None"]

# Line breaks other than LF that some editors and platforms emit.
_LINE_BREAKS = re.compile("\r\n|\r|\u0085| | ")
_HSPACE_RUN = re.compile("[ \t]+")
_URL = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?)]}'\""
_SLOT = re.compile(r"\{\{\s*(C-\d{4,})\s*\}\}")
_DEFAULT_PORTS = {"http": ":80", "https": ":443"}


class CanonicalFormError(ValueError):
    pass


# ---------------------------------------------------------------- text

def canonical_text(text: str) -> str:
    """NFC, LF line ends, runs of spaces and tabs collapsed to one space, each line trimmed of
    spaces and tabs, runs of blank lines collapsed to one, no leading or trailing blank lines.
    Every other character is kept: no-break spaces, zero-width joiners and case all count."""
    t = unicodedata.normalize("NFC", text)
    t = _LINE_BREAKS.sub("\n", t)
    lines = [_HSPACE_RUN.sub(" ", line).strip(" \t") for line in t.split("\n")]
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def outgoing_text(variant: Variant) -> str:
    """The text that goes out: the rendered text when there is one, else the source text."""
    return canonical_text(variant.rendered_text if variant.rendered_text is not None else variant.text)


def claim_slots(text: str) -> list[str]:
    """Claim codes named by {{C-0012}} slots, in order of first appearance."""
    seen: list[str] = []
    for code in _SLOT.findall(text):
        if code not in seen:
            seen.append(code)
    return seen


def has_unrendered_slot(text: str) -> bool:
    return bool(_SLOT.search(text))


# ---------------------------------------------------------------- links

def normalise_url(url: str) -> str:
    """Lowercase scheme and host, drop the default port, give an empty path "/". The path,
    query and fragment are kept byte for byte (no percent decoding)."""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)(.*)$", url)
    if not m:
        return url
    scheme, authority, rest = m.group(1).lower(), m.group(2), m.group(3)
    userinfo, _, host = authority.rpartition("@")
    host = host.lower()
    port = _DEFAULT_PORTS.get(scheme)
    if port and host.endswith(port):
        host = host[: -len(port)]
    authority = f"{userinfo}@{host}" if userinfo else host
    if rest == "" or rest[0] in "?#":
        rest = "/" + rest
    return f"{scheme}://{authority}{rest}"


def find_links(text: str) -> list[str]:
    """http and https URLs in order of appearance, trailing sentence punctuation removed."""
    return [m.group(0).rstrip(_URL_TRAILING) for m in _URL.finditer(text)]


def resolved_links(text: str, resolver: LinkResolver | None) -> list[list[str | None]]:
    out: list[list[str | None]] = []
    for raw in find_links(text):
        url = normalise_url(raw)
        dest = resolver(url) if resolver is not None else None
        out.append([url, normalise_url(dest) if dest is not None else None])
    return out


# ---------------------------------------------------------------- content snapshot

def sha256_bytes(data: bytes | Iterable[bytes]) -> str:
    h = hashlib.sha256()
    if isinstance(data, bytes | bytearray | memoryview):
        h.update(data)
    else:
        for chunk in data:
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class MediaItem:
    """One media file in a variant. `bytes_sha256` is computed from the bytes read from
    storage at the moment of hashing, never copied from the media row."""

    media: Media
    position: int
    bytes_sha256: str

    @classmethod
    def from_bytes(cls, media: Media, position: int, data: bytes | Iterable[bytes]) -> MediaItem:
        return cls(media=media, position=position, bytes_sha256=sha256_bytes(data))

    @property
    def matches_record(self) -> bool:
        return self.bytes_sha256 == self.media.sha256


@dataclass(frozen=True)
class VariantContent:
    variant: Variant
    media: tuple[MediaItem, ...] = ()
    claims: tuple[Claim, ...] = ()


@dataclass(frozen=True)
class BundleContent:
    bundle: Bundle
    variants: tuple[VariantContent, ...]

    def variant(self, variant_id: str) -> VariantContent | None:
        return next((v for v in self.variants if v.variant.id == variant_id), None)

    def foreign_rows(self) -> list[str]:
        """Rows that belong to another tenant or another bundle. Must be empty."""
        t, b = self.bundle.tenant_id, self.bundle.id
        out: list[str] = []
        for vc in self.variants:
            v = vc.variant
            if v.tenant_id != t or v.bundle_id != b:
                out.append(f"variant {v.id} belongs to another tenant or bundle")
            out += [f"media {m.media.id} belongs to another tenant" for m in vc.media if m.media.tenant_id != t]
            out += [f"claim {c.code} belongs to another tenant" for c in vc.claims if c.tenant_id != t]
        return out


@dataclass(frozen=True)
class BundleHash:
    root: str
    variants: Mapping[str, str] = field(default_factory=dict)  # variant id to leaf hash


# ---------------------------------------------------------------- encoding

def canonical_json(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8 without escaping, no NaN. Floats are refused."""
    _refuse_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _refuse_floats(value: Any) -> None:
    if isinstance(value, float):
        raise CanonicalFormError("floats are not allowed in canonical form; use a string or an int")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalFormError("canonical form keys must be strings")
            _refuse_floats(v)
    elif isinstance(value, list | tuple):
        for v in value:
            _refuse_floats(v)


def _digest(domain: bytes, value: Any) -> str:
    return hashlib.sha256(domain + canonical_json(value)).hexdigest()


def _claim_leaf(c: Claim) -> dict[str, Any]:
    return {
        "id": c.id,
        "code": c.code,
        "value": unicodedata.normalize("NFC", c.value),
        "unit": c.unit,
        "as_of": c.as_of.isoformat() if c.as_of else None,
        "evidence_sha256": c.evidence_sha256,
        "wording": {k: canonical_text(v) for k, v in sorted(c.languages.items())},
    }


def _media_leaf(m: MediaItem) -> dict[str, Any]:
    return {
        "position": m.position,
        "sha256": m.bytes_sha256,
        "kind": m.media.kind.value,
        "mime": m.media.mime.lower(),
        "alt_text": canonical_text(m.media.alt_text) if m.media.alt_text is not None else None,
        "disclosure_label": m.media.disclosure_label,
    }


def variant_leaf(vc: VariantContent, resolver: LinkResolver | None = None) -> dict[str, Any]:
    v = vc.variant
    claims = {c.id: c for c in vc.claims}  # a claim linked twice counts once
    media = sorted(vc.media, key=lambda m: (m.position, m.bytes_sha256, m.media.id))
    rendered = canonical_text(v.rendered_text) if v.rendered_text is not None else None
    return {
        "v": CANONICAL_VERSION,
        "variant_id": v.id,
        "channel": v.channel.value,
        "language": v.language.value,
        "text": canonical_text(v.text),
        "rendered_text": rendered,
        "links": resolved_links(outgoing_text(v), resolver),
        "media": [_media_leaf(m) for m in media],
        "claims": [_claim_leaf(c) for c in sorted(claims.values(), key=lambda c: (c.code, c.id))],
    }


def variant_hash(vc: VariantContent, resolver: LinkResolver | None = None) -> str:
    return _digest(VARIANT_DOMAIN, variant_leaf(vc, resolver))


def bundle_hash(content: BundleContent, resolver: LinkResolver | None = None) -> BundleHash:
    """The root the approval binds to. Variant order carries no meaning; media order does."""
    if not content.variants:
        raise CanonicalFormError("a bundle with no variants has no root hash")
    ids = [vc.variant.id for vc in content.variants]
    if len(set(ids)) != len(ids):
        raise CanonicalFormError("a variant appears twice in the bundle")
    leaves = {vc.variant.id: variant_hash(vc, resolver) for vc in content.variants}
    b = content.bundle
    root = _digest(ROOT_DOMAIN, {
        "v": CANONICAL_VERSION,
        "tenant_id": b.tenant_id,
        "bundle_id": b.id,
        "rule_version": b.rule_version,
        "variants": sorted(leaves.values()),
    })
    return BundleHash(root=root, variants=leaves)


def media_hashes(vc: VariantContent) -> tuple[str, ...]:
    """The media bytes hashes in position order, as a PublishRequest carries them."""
    return tuple(m.bytes_sha256 for m in sorted(vc.media, key=lambda m: (m.position, m.bytes_sha256, m.media.id)))


__all__: Sequence[str] = (
    "CANONICAL_VERSION", "BundleContent", "BundleHash", "CanonicalFormError", "LinkResolver", "MediaItem",
    "VariantContent", "bundle_hash", "canonical_json", "canonical_text", "claim_slots", "find_links",
    "has_unrendered_slot", "media_hashes", "normalise_url", "outgoing_text", "resolved_links", "sha256_bytes",
    "variant_hash", "variant_leaf",
)
