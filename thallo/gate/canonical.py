"""The canonical form and the bundle root hash. Written down in CANONICAL_FORM.md; keep both in step.

A bundle root hash covers, for every variant: its canonical text, every media file's bytes
(sha256 of the content, never the path) with its consent id, every used claim with its
evidence hash, every link in the outgoing text resolved to its destination, every tracked
link the variant references, its channel and language, plus the bundle's rule version, tenant
and id. Any change to any of these changes the root, which voids the approval bound to it.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from thallo.contracts.models import Bundle, Claim, Consent, Media, TrackedLink, Variant

CANONICAL_VERSION = 1

ROOT_DOMAIN = b"thallo/bundle-root/v1\n"
VARIANT_DOMAIN = b"thallo/variant/v1\n"

LinkResolver = Callable[[str], "str | None"]

# Line breaks other than LF that some editors and platforms emit.
_LINE_BREAKS = re.compile("\r\n|\r|\u0085| | ")
_HSPACE_RUN = re.compile("[ \t]+")
# ---- link detection (see CANONICAL_FORM.md, "Links"). Broad on purpose: fail closed.
_DOTS = str.maketrans({"。": ".", "｡": ".", "．": "."})  # NFKC already maps U+FF0E
_INVISIBLE = re.compile("[­\u200b‌‍⁠﻿]")
_TOKEN_SPLIT = re.compile(r"[\s<>\"]+")
# Inside a token without a scheme, these also separate pieces: a link can hide behind any of them.
_PIECE_SPLIT = re.compile("[,;|()\\[\\]{}!?'\"\u2018\u2019\u201c\u201d\u00ab\u00bb]+")
_LEAD = "([{'\"*_«‘“¿¡"
_TRAIL = ".,;:!?)]}'\"*_%»’”€$£"
_SCHEME_IN = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_IPV4 = re.compile(r"(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)")
_LABEL_RUN = re.compile(r"[\w-]+(?:\.[\w-]+)+")
_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)*")
_EMAIL = re.compile(r"([\w.+%-]+)@([\w-]+(?:\.[\w-]+)+)")
# Final labels that are common file extensions and are NOT real IANA top-level domains. md
# (Moldova), py (Paraguay), zip, mov and other real TLDs are deliberately absent: they stay links
# unless the tenant vouches for the token through its non-link allowlist.
FILE_EXTENSIONS = frozenset({"pdf", "js", "ts", "png", "jpg", "jpeg", "gif", "docx", "xlsx", "pptx", "csv", "txt"})
_TLD_FILE = Path(__file__).with_name("tlds.txt")


def _load_tlds() -> tuple[frozenset[str], bool]:
    names: set[str] = set()
    complete = False
    for raw in _TLD_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            if line.replace(" ", "").lower() == "#complete:yes":
                complete = True
            continue
        if line:
            names.add(line.lower())
    return frozenset(names), complete


# Real TLDs from gate/tlds.txt. While that file is a partial list, TLD_LIST_COMPLETE is False and the
# prose exemption ("word.Word") is off, so an unknown capitalised word after a dot fails closed.
GENERIC_TLDS, TLD_LIST_COMPLETE = _load_tlds()


def is_real_tld(label: str) -> bool:
    lab = label.lower()
    return (len(lab) == 2 and lab.isalpha()) or lab.startswith("xn--") or lab in GENERIC_TLDS
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


def link_key(token: str) -> str:
    """How a token is compared with a tenant's non-link allowlist: NFKC, dots unified, no invisible
    characters, case-folded."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", token).translate(_DOTS)).casefold()


def _strip(token: str) -> str:
    prev = None
    while token and token != prev:
        prev = token
        token = token.lstrip(_LEAD).rstrip(_TRAIL)
    return token


def _hostish(text: str) -> bool:
    """True when `text` holds a dot-label run ending in a real TLD."""
    return any(is_real_tld(m.group(0).split(".")[-1]) for m in _LABEL_RUN.finditer(text))


def _run_exempt(run: str, piece: str) -> bool:
    """Whether one dot-label run is plainly not a host, judged on its own."""
    labels = run.split(".")
    tld = labels[-1]
    if all(lab.isdigit() for lab in labels):
        return len(labels) < 4  # a number such as 1.200; four numeric labels is IPv4
    if _NUMBER.fullmatch(run):
        return True
    if len(tld) < 2 or tld.lower() in FILE_EXTENSIONS:
        return True
    return (TLD_LIST_COMPLETE and len(labels) == 2 and run == piece and labels[0].isalpha() and tld.isalpha()
            and tld[0].isupper() and tld[1:].islower() and not is_real_tld(tld))


def _piece_links(piece: str) -> list[str]:
    """Links in one piece (no scheme inside). Every dot-run is checked; the first that is not
    exempt starts a link, and the rest of the piece is that link's path."""
    if not piece:
        return []
    if piece.startswith("//"):
        return [normalise_url("https:" + piece)]
    # The link starts at the EARLIEST host candidate of any kind, so a later IPv4 or www. match
    # can never hide an earlier host (for example "evil.com/1.2.3.4").
    starts: list[int] = []
    if (m := _IPV4.search(piece)) is not None:
        starts.append(m.start())
    if (w := re.search(r"(?i)(?<![\w.])www\.", piece)) is not None:
        starts.append(w.start())
    for run in _LABEL_RUN.finditer(piece):
        if not _run_exempt(run.group(0), piece):
            starts.append(run.start())
            break
    if not starts:
        return []
    return [normalise_url("https://" + piece[min(starts):].lstrip("-_"))]


def find_link_tokens(text: str) -> list[tuple[str, str]]:
    """(token, normalised url) for every link in the text, in order of appearance."""
    prepared = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text).translate(_DOTS))
    out: list[tuple[str, str]] = []
    for raw in _TOKEN_SPLIT.split(prepared):
        token = _strip(raw)
        if not token:
            continue
        em = _EMAIL.fullmatch(token)
        if em and not _hostish(em.group(1)):
            continue  # the whole token is exactly one plain email address
        m = _SCHEME_IN.search(token)
        head = token[: m.start()] if m else token
        for part in _PIECE_SPLIT.split(head):
            piece = _strip(part)
            out += [(piece, url) for url in _piece_links(piece)]
        if m:
            url_text = token[m.start():]
            out.append((url_text, normalise_url(url_text)))
    return out


def find_links(text: str) -> list[str]:
    """Every link in the text, normalised, in order of appearance. Matches broadly: any scheme,
    `//`, `www.`, IPv4, and any dot-separated label run (Unicode, full-width and ideographic dots
    included), except numbers, email addresses, a final label shorter than two characters, a
    final label that is a common file extension and not a real TLD, and "word.Word" prose."""
    return [url for _, url in find_link_tokens(text)]


class MemoResolver:
    """Resolves each URL once and remembers the answer, so the hash and the payload can never see
    two different destinations for the same link."""

    def __init__(self, resolver: LinkResolver) -> None:
        if resolver is None:
            raise CanonicalFormError("a link resolver is required")
        self._resolver = resolver
        self._seen: dict[str, str | None] = {}

    def __call__(self, url: str) -> str | None:
        if url not in self._seen:
            dest = self._resolver(url)
            if dest is not None and not isinstance(dest, str):
                raise CanonicalFormError(f"resolver returned {type(dest).__name__} for {url}")
            self._seen[url] = dest
        return self._seen[url]


def resolved_links(text: str, resolver: LinkResolver) -> list[list[str | None]]:
    """[url, destination] per link. The destination is None when the resolver does not know it."""
    out: list[list[str | None]] = []
    for url in find_links(text):
        dest = resolver(url)
        if dest is not None and not isinstance(dest, str):
            raise CanonicalFormError(f"resolver returned {type(dest).__name__} for {url}")
        out.append([url, normalise_url(dest) if dest is not None else None])
    return out


def valid_non_links(non_links: frozenset[str]) -> frozenset[str]:
    """Allowlist keys the gate honours. An entry whose last label is a real TLD is ignored: a
    plausible host must resolve, never be waved through."""
    keys = {link_key(x) for x in non_links}
    # Judge each entry the way the link finder would: any label run ending in a real TLD, any
    # scheme, userinfo, port, IPv4 or www. prefix makes it a plausible host, which is refused.
    return frozenset(k for k in keys if not (
        _hostish(k) or "://" in k or k.startswith("//") or "@" in k or ":" in k
        or _IPV4.search(k) or k.lower().startswith("www.")))


def vouched_tokens(text: str, non_links: frozenset[str]) -> list[str]:
    """The allowlist keys this text actually relies on, sorted. Bound into the variant hash."""
    keys = valid_non_links(non_links)
    return sorted({link_key(tok) for tok, url in find_link_tokens(text) if link_key(tok) in keys}
                  | {link_key(url) for tok, url in find_link_tokens(text) if link_key(url) in keys})


def _vouched(text: str, non_links: frozenset[str]) -> set[str]:
    keys = valid_non_links(non_links)
    return {url for tok, url in find_link_tokens(text) if link_key(tok) in keys or link_key(url) in keys}


def unresolved(text: str, links: list[list[str | None]], non_links: frozenset[str]) -> list[str]:
    """Links that neither resolve nor are on the tenant's non-link allowlist."""
    allowed = _vouched(text, non_links)
    return [u for u, d in links if d is None and u not in allowed]  # type: ignore[misc]


def find_unresolved_links(text: str, resolver: LinkResolver, non_links: frozenset[str] = frozenset()) -> list[str]:
    """For early copy checks (WP-07): every link in `text` that would block approval and publish."""
    return unresolved(text, resolved_links(text, MemoResolver(resolver)), non_links)


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
    storage at the moment of hashing, never copied from the media row. `consent` is the row
    named by media.consent_id, loaded for the publish-time check (it is not hashed; the id is)."""

    media: Media
    position: int
    bytes_sha256: str
    consent: Consent | None = None

    @classmethod
    def from_bytes(cls, media: Media, position: int, data: bytes | Iterable[bytes],
                   consent: Consent | None = None) -> MediaItem:
        return cls(media=media, position=position, bytes_sha256=sha256_bytes(data), consent=consent)

    @property
    def matches_record(self) -> bool:
        return self.bytes_sha256 == self.media.sha256


@dataclass(frozen=True)
class VariantContent:
    variant: Variant
    media: tuple[MediaItem, ...] = ()
    claims: tuple[Claim, ...] = ()
    tracked_links: tuple[TrackedLink, ...] = ()


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
            out += [f"consent {m.consent.id} belongs to another tenant" for m in vc.media
                    if m.consent is not None and m.consent.tenant_id != t]
            out += [f"claim {c.code} belongs to another tenant" for c in vc.claims if c.tenant_id != t]
            out += [f"tracked link {tl.slug} belongs to another tenant or variant" for tl in vc.tracked_links
                    if tl.tenant_id != t or tl.variant_id != v.id]
        return out


@dataclass(frozen=True)
class BundleHash:
    root: str
    variants: Mapping[str, str] = field(default_factory=dict)  # variant id to leaf hash


@dataclass(frozen=True)
class PayloadMedia:
    position: int
    sha256: str
    kind: str
    mime: str
    alt_text: str | None
    disclosure_label: str | None
    consent_id: str | None
    # Not hashed and not sent: where the file came from (media.provenance), so the gate can pick
    # the checks this item needs (gate/checks.py). None when no single media row matches.
    provenance: str | None = None


@dataclass(frozen=True)
class PublishPayload:
    """Exactly what was hashed for one variant, and so exactly what may be sent. Frozen."""

    variant_id: str
    variant_hash: str
    channel: str
    language: str
    text: str
    media: tuple[PayloadMedia, ...]
    links: tuple[tuple[str, str | None], ...]
    tracked_links: tuple[tuple[str, str, str | None], ...]

    @property
    def media_sha256(self) -> tuple[str, ...]:
        return tuple(m.sha256 for m in self.media)


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
        "version": c.version,
        "supersedes_id": c.supersedes_id,
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
        "consent_id": m.media.consent_id,
    }


def _sorted_media(vc: VariantContent) -> list[MediaItem]:
    return sorted(vc.media, key=lambda m: (m.position, m.bytes_sha256, m.media.id))


def variant_leaf(vc: VariantContent, resolver: LinkResolver,
                 non_links: frozenset[str] = frozenset()) -> dict[str, Any]:
    v = vc.variant
    claims = {c.id: c for c in vc.claims}  # a claim linked twice counts once
    rendered = canonical_text(v.rendered_text) if v.rendered_text is not None else None
    tracked = sorted({(tl.slug, normalise_url(tl.destination), tl.whatsapp_code) for tl in vc.tracked_links},
                     key=lambda t: (t[0], t[1], t[2] or ""))
    return {
        "v": CANONICAL_VERSION,
        "variant_id": v.id,
        "channel": v.channel.value,
        "language": v.language.value,
        "text": canonical_text(v.text),
        "rendered_text": rendered,
        "links": resolved_links(outgoing_text(v), resolver),
        "vouched_non_links": vouched_tokens(outgoing_text(v), non_links),
        "tracked_links": [list(t) for t in tracked],
        "media": [_media_leaf(m) for m in _sorted_media(vc)],
        "claims": [_claim_leaf(c) for c in sorted(claims.values(), key=lambda c: (c.code, c.id))],
    }


def variant_hash(vc: VariantContent, resolver: LinkResolver, non_links: frozenset[str] = frozenset()) -> str:
    """The variant's leaf hash: the value `build_snapshot` puts in `BundleHash.variants` and in the
    root, and the value every `check_result.content_sha256` must carry. Pass the same resolver and
    non-link allowlist the gate uses (the repository's), or the hash will not match."""
    memo = resolver if isinstance(resolver, MemoResolver) else MemoResolver(resolver)
    return _digest(VARIANT_DOMAIN, variant_leaf(vc, memo, frozenset(non_links)))


@dataclass(frozen=True)
class Snapshot:
    """One resolution of a bundle: every leaf built once, with one memoised resolver. The root
    hash and every publish payload derive from these same leaves."""

    content: BundleContent
    leaves: Mapping[str, Mapping[str, Any]]
    hash: BundleHash
    non_links: frozenset[str] = frozenset()

    def payload(self, variant_id: str) -> PublishPayload:
        vc = self.content.variant(variant_id)
        if vc is None:
            raise CanonicalFormError("variant is not in this snapshot")
        return _payload_from_leaf(vc, self.leaves[variant_id])

    def unresolved(self, variant_id: str, non_links: frozenset[str]) -> list[str]:
        vc = self.content.variant(variant_id)
        if vc is None:
            return []
        return unresolved(outgoing_text(vc.variant), list(self.leaves[variant_id]["links"]), self.non_links)


def build_snapshot(content: BundleContent, resolver: LinkResolver,
                   non_links: frozenset[str] = frozenset()) -> Snapshot:
    """`resolver` is required: approval and publish must resolve links the same way, so both load
    through the repository (`load_bundle`) and pass `repo.resolve_link`."""
    memo = resolver if isinstance(resolver, MemoResolver) else MemoResolver(resolver)
    if not content.variants:
        raise CanonicalFormError("a bundle with no variants has no root hash")
    ids = [vc.variant.id for vc in content.variants]
    if len(set(ids)) != len(ids):
        raise CanonicalFormError("a variant appears twice in the bundle")
    non_links = frozenset(non_links)
    leaves = {vc.variant.id: variant_leaf(vc, memo, non_links) for vc in content.variants}
    hashes = {vid: _digest(VARIANT_DOMAIN, leaf) for vid, leaf in leaves.items()}
    b = content.bundle
    root = _digest(ROOT_DOMAIN, {
        "v": CANONICAL_VERSION,
        "tenant_id": b.tenant_id,
        "bundle_id": b.id,
        "rule_version": b.rule_version,
        "variants": sorted(hashes.values()),
    })
    return Snapshot(content=content, leaves=leaves, hash=BundleHash(root=root, variants=hashes),
                    non_links=non_links)


def bundle_hash(content: BundleContent, resolver: LinkResolver,
                non_links: frozenset[str] = frozenset()) -> BundleHash:
    """The root the approval binds to. Variant order carries no meaning; media order does."""
    return build_snapshot(content, resolver, non_links).hash


def publish_payload(vc: VariantContent, resolver: LinkResolver) -> PublishPayload:
    """The frozen payload for one variant, built from the same leaf that is hashed."""
    return _payload_from_leaf(vc, variant_leaf(vc, MemoResolver(resolver)))


def _payload_from_leaf(vc: VariantContent, leaf: Mapping[str, Any]) -> PublishPayload:
    return PublishPayload(
        variant_id=leaf["variant_id"],
        variant_hash=_digest(VARIANT_DOMAIN, leaf),
        channel=leaf["channel"],
        language=leaf["language"],
        text=outgoing_text(vc.variant),
        media=tuple(PayloadMedia(**m, provenance=_provenance(vc, m)) for m in leaf["media"]),
        links=tuple((u, d) for u, d in leaf["links"]),
        tracked_links=tuple((s, d, w) for s, d, w in leaf["tracked_links"]),
    )


def _provenance(vc: VariantContent, leaf_media: Mapping[str, Any]) -> str | None:
    """The provenance of the media row a leaf entry came from, matched on position and bytes
    hash. None when no single provenance matches: never guessed."""
    found = {m.media.provenance.value for m in vc.media
             if m.position == leaf_media["position"] and m.bytes_sha256 == leaf_media["sha256"]}
    return found.pop() if len(found) == 1 else None


def media_hashes(vc: VariantContent) -> tuple[str, ...]:
    """The media bytes hashes in position order, as a PublishRequest carries them."""
    return tuple(m.bytes_sha256 for m in _sorted_media(vc))


__all__ = [
    "CANONICAL_VERSION", "FILE_EXTENSIONS", "GENERIC_TLDS", "TLD_LIST_COMPLETE", "BundleContent", "BundleHash",
    "CanonicalFormError", "LinkResolver", "MediaItem", "MemoResolver", "PayloadMedia", "PublishPayload", "Snapshot",
    "VariantContent", "build_snapshot", "bundle_hash", "canonical_json", "canonical_text", "claim_slots",
    "find_link_tokens", "find_links", "find_unresolved_links", "has_unrendered_slot", "is_real_tld", "link_key",
    "media_hashes", "normalise_url", "outgoing_text", "publish_payload", "resolved_links", "sha256_bytes",
    "unresolved", "valid_non_links", "variant_hash", "variant_leaf", "vouched_tokens",
]
