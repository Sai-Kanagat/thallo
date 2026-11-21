"""The claim pack file: a closed schema, canonical bytes, an Ed25519 signature, and export.

A pack carries manufacturer facts only, never a client's own claims or anyone's personal data.
That is enforced by allow-list, not by hoping the text is clean:

- Only claims a person picked by code, each verified, unexpired and the latest version.
- Kinds: NUMBER, DATE, NAME, CAPABILITY. PRICE and OFFER are a dealer's own terms and QUOTE is a
  person's words, so all three are refused.
- The source must be a DOCUMENT or WEB_PAGE (a spec sheet, a product page), trusted, with no
  consent attached (consent means a customer's proof asset). Voice answers, interviews, seller
  notes, typed notes and research are refused.
- The file has a closed schema (`extra="forbid"`): kind, value, unit, as_of, approved wording and
  the source's title, public https address and capture date. No tenant, user, source or claim
  id, no verifier, no source span, no evidence bytes, no model fields.
- Free text (values of NAME and CAPABILITY, wording, source titles) is scanned for email
  addresses, phone numbers, handles and links. Code cannot prove free text is clean; the scan
  catches the common leaks and the person exporting still chooses each claim.

Tamper detection: the pack body is serialised to canonical JSON and signed with Ed25519 by the
publisher's key. An importer checks the signature against publisher keys it already trusts,
passed in from outside; a key carried in the file is never used, so editing the file and
re-signing with another key fails. A digest inside the file would prove nothing, since an editor
can recompute it.

Expiry: every pack has one expiry, no later than the earliest expiry of any claim in it and at
most 365 days out. Every imported claim takes that expiry, so pack claims expire together.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from thallo.contracts.clock import Clock
from thallo.contracts.enums import ClaimKind, Language, SourceKind
from thallo.contracts.models import Claim, Source
from thallo.truth.claims import ClaimError, check_bound, check_value, problem

FORMAT = "thallo-claim-pack/1"
SIGN_DOMAIN = b"thallo/claim-pack/v1\n"
MAX_LIFETIME = timedelta(days=365)
MAX_ITEMS = 200
ALLOWED_KINDS = frozenset({ClaimKind.NUMBER, ClaimKind.DATE, ClaimKind.NAME, ClaimKind.CAPABILITY})
ALLOWED_SOURCES = frozenset({SourceKind.DOCUMENT, SourceKind.WEB_PAGE})

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_HANDLE = re.compile(r"(?:^|\s)@\w")
_LINK = re.compile(r"(?i)\b(?:https?://|www\.)")
_URI = re.compile(r"^https://[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::[0-9]{2,5})?(?:/[A-Za-z0-9._~%!$&'()*+,;=:-]*)*$")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


class PackError(ValueError):
    pass


# ---------------------------------------------------------------- schema

class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PackSource(_Closed):
    title: str = Field(min_length=1, max_length=200)
    uri: str = Field(min_length=9, max_length=500)
    captured_on: date


class PackItem(_Closed):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    kind: ClaimKind
    value: str = Field(min_length=1, max_length=300)
    unit: str | None = Field(default=None, max_length=16)
    as_of: date | None = None
    languages: dict[str, str] = Field(default_factory=dict)
    source: PackSource


class PackBody(_Closed):
    format: Literal["thallo-claim-pack/1"] = FORMAT
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    title: str = Field(min_length=1, max_length=120)
    publisher: str = Field(min_length=1, max_length=120)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    languages: list[Language] = Field(min_length=1)
    items: list[PackItem] = Field(min_length=1, max_length=MAX_ITEMS)

    @field_validator("items")
    @classmethod
    def _unique_keys(cls, items: list[PackItem]) -> list[PackItem]:
        keys = [i.key for i in items]
        if len(set(keys)) != len(keys):
            raise ValueError("item keys must be unique")
        return items


class SignedPack(_Closed):
    """The file. `body` stays a plain mapping until the signature over it has been checked."""

    format: Literal["thallo-claim-pack/1"] = FORMAT
    key_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    signature: str = Field(min_length=1, max_length=200)
    body: dict[str, Any]


# ---------------------------------------------------------------- canonical bytes and keys

def canonical(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8. The pack body holds only strings, ints, lists and maps."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(body)).hexdigest()


def key_id_of(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


class PackSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


@dataclass(frozen=True)
class Ed25519Signer:
    """The publisher's signing key. In production the private key comes from the vault and never
    from a setting, a prompt or the database; tests generate one."""

    private: Ed25519PrivateKey

    @property
    def key_id(self) -> str:
        return key_id_of(self.private.public_key())

    def sign(self, message: bytes) -> bytes:
        return self.private.sign(message)


class TrustedKeys:
    """Publisher public keys an importer accepts, by key id. Public keys are not secrets."""

    def __init__(self, keys: Sequence[Ed25519PublicKey] = ()) -> None:
        self._keys = {key_id_of(k): k for k in keys}

    @classmethod
    def from_raw(cls, raw_keys: Sequence[bytes]) -> TrustedKeys:
        return cls([Ed25519PublicKey.from_public_bytes(r) for r in raw_keys])

    @classmethod
    def from_text(cls, text: str | None) -> TrustedKeys:
        """Comma separated base64 raw 32-byte public keys."""
        parts = [p.strip() for p in (text or "").split(",") if p.strip()]
        try:
            return cls.from_raw([base64.b64decode(p, validate=True) for p in parts])
        except ValueError as exc:
            raise PackError("a trusted pack key is not a base64 Ed25519 public key") from exc

    def get(self, key_id: str) -> Ed25519PublicKey | None:
        return self._keys.get(key_id)

    def __len__(self) -> int:
        return len(self._keys)


def sign_body(body: PackBody, signer: PackSigner) -> SignedPack:
    raw = body.model_dump(mode="json")
    sig = signer.sign(SIGN_DOMAIN + canonical(raw))
    return SignedPack(key_id=signer.key_id, signature=base64.b64encode(sig).decode("ascii"), body=raw)


def open_pack(data: bytes | str | Mapping[str, Any], trusted: TrustedKeys) -> tuple[PackBody, str, str]:
    """Check the signature, then parse the body. Returns (body, digest, key_id). Nothing in the
    body is read before the signature verifies against a key in `trusted`."""
    try:
        raw = json.loads(data) if isinstance(data, (bytes, str)) else dict(data)
        envelope = SignedPack.model_validate(raw)
    except (ValueError, ValidationError) as exc:
        raise PackError("not a claim pack file") from exc
    public = trusted.get(envelope.key_id)
    if public is None:
        raise PackError("the pack is signed by a key this workspace does not trust")
    try:
        signature = base64.b64decode(envelope.signature, validate=True)
        public.verify(signature, SIGN_DOMAIN + canonical(envelope.body))
    except (ValueError, InvalidSignature) as exc:
        raise PackError("the pack signature does not verify: the file was changed or signed by someone else") from exc
    try:
        body = PackBody.model_validate(envelope.body)
    except ValidationError as exc:
        raise PackError(f"the pack body is malformed: {exc.errors()[0]['msg']}") from exc
    if canonical(body.model_dump(mode="json")) != canonical(envelope.body):
        raise PackError("the pack body is not in canonical form")
    return body, digest(envelope.body), envelope.key_id


# ---------------------------------------------------------------- privacy and content rules

def text_problems(text: str) -> list[str]:
    out = []
    if _EMAIL.search(text):
        out.append("an email address")
    if _HANDLE.search(text):
        out.append("a social handle")
    if _LINK.search(text):
        out.append("a link")
    if _PHONE.search(text):
        out.append("a phone number")
    return out


def check_uri(uri: str) -> None:
    if not _URI.match(uri):
        raise PackError(f"source address must be a public https address with no login, query or fragment, "
                        f"got {uri!r}")


def check_item(item: PackItem, languages: Collection[Language]) -> None:
    """The content rules, run on export and again on import (an importer trusts no exporter)."""
    if item.kind not in ALLOWED_KINDS:
        raise PackError(f"{item.key}: a {item.kind.value} claim cannot travel in a pack")
    try:
        check_value(item.kind, item.value, item.unit)
    except ClaimError as exc:
        raise PackError(f"{item.key}: {exc}") from exc
    allowed = {lang.value for lang in Language}
    bad = sorted(set(item.languages) - allowed)
    if bad:
        raise PackError(f"{item.key}: unknown language codes {bad}")
    texts = [("source title", item.source.title)]
    texts += [(f"{lang} wording", w) for lang, w in item.languages.items()]
    if item.kind in (ClaimKind.NAME, ClaimKind.CAPABILITY):
        texts.append(("value", item.value))
    for label, text in texts:
        found = text_problems(text)
        if found:
            raise PackError(f"{item.key}: {label} contains {', '.join(found)}; packs carry no personal data")
    check_uri(item.source.uri)
    if item.kind is ClaimKind.NUMBER and item.unit is None:
        missing = [lang.value for lang in languages if not (item.languages.get(lang.value) or "").strip()]
        if missing:
            raise PackError(f"{item.key}: a number without a unit needs approved wording in {', '.join(missing)}")


def check_window(issued_at: datetime, expires_at: datetime) -> None:
    if expires_at <= issued_at:
        raise PackError("pack expiry must be after the issue date")
    if expires_at > issued_at + MAX_LIFETIME:
        raise PackError(f"a pack may live at most {MAX_LIFETIME.days} days")


# ---------------------------------------------------------------- export

def _item_key(index: int) -> str:
    return f"item-{index:03d}"


def export_pack(*, claims: Mapping[str, Claim], sources: Mapping[str, Source], codes: Sequence[str], pack_id: str,
                title: str, publisher: str, expires_at: datetime, languages: Sequence[Language], clock: Clock,
                signer: PackSigner) -> SignedPack:
    """A signed pack of the picked claims. `claims` is the tenant's code-keyed library of latest
    versions (`truth.library`), `sources` its sources by id. Refuses the whole pack on the first
    claim that may not travel, naming it, rather than silently dropping it."""
    now = clock.now()
    if not codes:
        raise PackError("pick at least one claim")
    if len(set(codes)) != len(codes):
        raise PackError("a claim is picked twice")
    if len(codes) > MAX_ITEMS:
        raise PackError(f"a pack holds at most {MAX_ITEMS} claims")
    check_window(now, expires_at)
    items = []
    for index, code in enumerate(codes, start=1):
        claim = claims.get(code)
        why = problem(claim, now)
        if claim is None or why is not None:
            raise PackError(f"{code}: {why or 'unknown claim'}; only verified, live claims can be exported")
        if claim.kind not in ALLOWED_KINDS:
            raise PackError(f"{code}: a {claim.kind.value} claim cannot travel in a pack")
        if claim.expires_at is not None and claim.expires_at < expires_at:
            raise PackError(f"{code} expires at {claim.expires_at.isoformat()}, before the pack would; "
                            "set the pack expiry on or before that date")
        source = sources.get(claim.source_id) if claim.source_id else None
        if source is None:
            raise PackError(f"{code}: no source; a pack claim needs a spec sheet or product page behind it")
        if source.kind not in ALLOWED_SOURCES or source.untrusted or source.consent_id is not None:
            raise PackError(f"{code}: its source is a {source.kind.value}; only a manufacturer document or web page "
                            "without customer consent can back a pack claim")
        if not source.uri:
            raise PackError(f"{code}: its source has no public address")
        try:
            check_bound(claim, languages)
        except ClaimError as exc:
            raise PackError(str(exc)) from exc
        wording = {lang.value: claim.languages[lang.value] for lang in languages
                   if (claim.languages.get(lang.value) or "").strip()}
        try:
            item = PackItem(key=_item_key(index), kind=claim.kind, value=claim.value, unit=claim.unit,
                            as_of=claim.as_of, languages=wording,
                            source=PackSource(title=source.title, uri=source.uri,
                                              captured_on=source.captured_at.date()))
        except ValidationError as exc:
            raise PackError(f"{code}: {exc.errors()[0]['msg']}") from exc
        check_item(item, languages)
        items.append(item)
    try:
        body = PackBody(pack_id=pack_id, title=title, publisher=publisher, issued_at=now, expires_at=expires_at,
                        languages=list(languages), items=items)
    except ValidationError as exc:
        raise PackError(f"pack details: {exc.errors()[0]['msg']}") from exc
    for label, text in (("title", title), ("publisher", publisher)):
        if text_problems(text):
            raise PackError(f"pack {label} contains personal or contact data")
    return sign_body(body, signer)


def to_bytes(pack: SignedPack) -> bytes:
    return canonical(pack.model_dump(mode="json"))
