"""Minting tracked links and codes, inside one tenant transaction.

`tx` is anything with `tenant_id`, `insert(row)` and `list_all(model, **where)`: the store's
`TenantRepo` or the API's tenant transaction. Mint links BEFORE the variant is checked and
approved: tracked links are part of the variant hash (gate/CANONICAL_FORM.md), so a link added
later voids the approval, as it should.

The public URL of a link is `<prefix><slug>`, where the prefix is one of THALLO_REDIRECT_PREFIXES.
That is exactly the shape `PgGateRepository.resolve_link` resolves through this tenant's
`tracked_link` row, so the gate shows the reviewer the stored destination, and the redirector
(thallo/tracking/redirector.py) sends the browser to that same stored destination.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from thallo.contracts.clock import Clock
from thallo.contracts.ids import new_id
from thallo.contracts.models import Row, TrackedLink
from thallo.gate.canonical import normalise_url
from thallo.tracking.codes import new_code, new_slug
from thallo.tracking.destinations import check_destination, with_ref
from thallo.tracking.models import CodeKind, TrackingCode

_PREFIX = re.compile(r"^https://[a-z0-9.-]+(:\d{1,5})?/([A-Za-z0-9._~-]+/)*$")
_PHONE = re.compile(r"^\+?(\d{6,15})$")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
MAX_GREETING = 200


class Tx(Protocol):
    tenant_id: str

    def insert(self, row: Row) -> Row: ...
    def list_all(self, model: type, **where: Any) -> list: ...


class BadPrefix(ValueError):
    pass


@dataclass(frozen=True)
class MintedLink:
    link: TrackedLink
    public_url: str  # what goes in the post, on the flyer or in the QR code
    code: TrackingCode | None = None  # the QR code row, for a QR link


def check_prefix(prefix: str) -> str:
    """A redirect prefix: https, plain host, path ending in `/`. Normalised as the gate does."""
    p = normalise_url(prefix.strip())
    if not _PREFIX.match(p):
        raise BadPrefix("a redirect prefix is https://host/path/ ending in a slash")
    return p


def public_url(prefix: str, slug: str) -> str:
    return check_prefix(prefix) + slug


def _link(tx: Tx, clock: Clock, prefix: str, destination: str, *, variant_id: str | None, ad_id: str | None,
          add_ref: bool, whatsapp_code: str | None = None) -> MintedLink:
    prefix = check_prefix(prefix)
    dest = check_destination(destination)
    slug = new_slug()
    if add_ref:
        dest = check_destination(with_ref(dest, slug))
    row = TrackedLink(id=new_id(clock), created_at=clock.now(), tenant_id=tx.tenant_id, variant_id=variant_id,
                      ad_id=ad_id, slug=slug, destination=dest, whatsapp_code=whatsapp_code)
    tx.insert(row)
    return MintedLink(row, prefix + slug)


def mint_link(tx: Tx, clock: Clock, *, prefix: str, destination: str, variant_id: str | None = None,
              ad_id: str | None = None, add_ref: bool = True) -> MintedLink:
    """A tracked link to a landing page. With `add_ref`, the destination carries
    `thallo_ref=<slug>` so the page's form can return it in a hidden field."""
    return _link(tx, clock, prefix, destination, variant_id=variant_id, ad_id=ad_id, add_ref=add_ref)


def _quote(text: str) -> str:
    return "".join(ch if ch in _UNRESERVED else "".join(f"%{b:02X}" for b in ch.encode()) for ch in text)


def whatsapp_text(greeting: str, code: str) -> str:
    return f"{greeting.strip()} (ref {code})" if greeting.strip() else f"ref {code}"


def mint_whatsapp_link(tx: Tx, clock: Clock, *, prefix: str, business_number: str, greeting: str = "",
                       variant_id: str | None = None, ad_id: str | None = None) -> MintedLink:
    """A click-to-chat link whose prefilled message carries a reference code. Two signals: the
    click on the redirect, and the code in the chat (kept even if the click was not counted).
    `business_number` is the business's own WhatsApp number, never a lead's."""
    m = _PHONE.match(business_number.replace(" ", ""))
    if not m:
        raise ValueError("business_number must be 6 to 15 digits, optionally with a leading +")
    if len(greeting) > MAX_GREETING:
        raise ValueError("greeting is too long")
    code = _fresh_code(tx)
    dest = f"https://wa.me/{m.group(1)}?text={_quote(whatsapp_text(greeting, code))}"
    return _link(tx, clock, prefix, dest, variant_id=variant_id, ad_id=ad_id, add_ref=False, whatsapp_code=code)


def mint_qr(tx: Tx, clock: Clock, *, prefix: str, destination: str, label: str | None = None,
            variant_id: str | None = None, ad_id: str | None = None) -> MintedLink:
    """A QR code is its own tracked link (so a scan is a click on that link) plus a `qr` code row
    naming it, so reports can tell scans from clicks. The QR payload is `public_url`."""
    minted = _link(tx, clock, prefix, destination, variant_id=variant_id, ad_id=ad_id, add_ref=True)
    code = TrackingCode(id=new_id(clock), created_at=clock.now(), tenant_id=tx.tenant_id, code=_fresh_code(tx),
                        kind=CodeKind.QR, variant_id=variant_id, ad_id=ad_id, tracked_link_id=minted.link.id,
                        label=label)
    tx.insert(code)
    return MintedLink(minted.link, minted.public_url, code)


def mint_code(tx: Tx, clock: Clock, *, kind: CodeKind, label: str | None = None, variant_id: str | None = None,
              ad_id: str | None = None) -> TrackingCode:
    """An offer, walk-in or call code. QR codes come from `mint_qr`."""
    if kind is CodeKind.QR:
        raise ValueError("a QR code needs its own link: use mint_qr")
    row = TrackingCode(id=new_id(clock), created_at=clock.now(), tenant_id=tx.tenant_id, code=_fresh_code(tx),
                       kind=kind, variant_id=variant_id, ad_id=ad_id, label=label)
    tx.insert(row)
    return row


def _fresh_code(tx: Tx) -> str:
    """A code not yet used by this tenant, as a WhatsApp code or a tracking code. WhatsApp codes
    are also unique across tenants (the database refuses a clash; 22^6 makes one unlikely)."""
    for _ in range(20):
        code = new_code()
        if not tx.list_all(TrackedLink, whatsapp_code=code) and not tx.list_all(TrackingCode, code=code):
            return code
    raise RuntimeError("could not find a free code")


__all__ = ["BadPrefix", "MintedLink", "check_prefix", "mint_code", "mint_link", "mint_qr", "mint_whatsapp_link",
           "public_url", "whatsapp_text"]
