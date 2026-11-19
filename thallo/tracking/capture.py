"""Every entry point turns into a `touch` tied to the variant or ad that caused it.

Entry points, each inside one tenant transaction (`tx` as in links.py):
- click: written by the redirector (`record_click`), kind `link`;
- WhatsApp chat: `capture_whatsapp(text)`, kind `whatsapp_code`, from a reference code in the message;
- form: `capture_form(fields)`, kind `form`, from the hidden `thallo_ref` field or a typed code,
  plus a `self_report` touch when the form asked "how did you hear about us";
- phone call: `capture_call(code)`, kind `call`;
- walk-in: `capture_walk_in(code)`, kind `walk_in_code`;
- self-report on its own: `capture_self_report(answer)`, kind `self_report`.

What is stored: the kind, the ids it resolves to, the matched code or option key, and the time.
Never an IP address, never the message body, never a raw form value: a code is stored only when it
is one of this tenant's real codes, so untrusted text never reaches the touch row. Lookups go
through the tenant's own rows, so another tenant's code or slug resolves to nothing.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from thallo.contracts.clock import Clock
from thallo.contracts.enums import TouchKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import Touch, TrackedLink
from thallo.tracking.codes import code_candidates, is_slug, normalise_code
from thallo.tracking.destinations import REF_PARAM
from thallo.tracking.links import Tx
from thallo.tracking.models import TrackingCode
from thallo.tracking.selfreport import normalise_answer

CODE_FIELD = "thallo_code"
HOW_HEARD_FIELD = "how_heard"


@dataclass(frozen=True)
class Source:
    variant_id: str | None
    ad_id: str | None
    tracked_link_id: str | None
    code: str | None


def form_fields(link: TrackedLink) -> dict[str, str]:
    """Hidden inputs for a form reached from this link. A landing page can instead copy
    `thallo_ref` from its own URL (the link's destination carries it)."""
    return {REF_PARAM: link.slug}


def find_code(tx: Tx, raw: object) -> Source | None:
    """One of this tenant's codes: a WhatsApp reference first, then an active tracking code."""
    code = normalise_code(raw)
    if code is None:
        return None
    links = tx.list_all(TrackedLink, whatsapp_code=code)
    if links:
        link = links[0]
        return Source(link.variant_id, link.ad_id, link.id, code)
    rows = [r for r in tx.list_all(TrackingCode, code=code) if r.active]
    if rows:
        r = rows[0]
        return Source(r.variant_id, r.ad_id, r.tracked_link_id, code)
    return None


def find_slug(tx: Tx, slug: object) -> Source | None:
    if not is_slug(slug):
        return None
    links = tx.list_all(TrackedLink, slug=slug)
    return Source(links[0].variant_id, links[0].ad_id, links[0].id, None) if links else None


def _touch(tx: Tx, clock: Clock, kind: TouchKind, source: Source | None, *, person_id: str | None,
           self_report: str | None = None) -> Touch:
    s = source or Source(None, None, None, None)
    row = Touch(id=new_id(clock), created_at=clock.now(), tenant_id=tx.tenant_id, kind=kind, person_id=person_id,
                variant_id=s.variant_id, ad_id=s.ad_id, tracked_link_id=s.tracked_link_id, code=s.code,
                self_report=self_report, occurred_at=clock.now())
    tx.insert(row)
    return row


def record_click(tx: Tx, clock: Clock, link: TrackedLink) -> Touch:
    """A counted click on a tracked link. No person: a click is anonymous."""
    return _touch(tx, clock, TouchKind.LINK, Source(link.variant_id, link.ad_id, link.id, None), person_id=None)


def capture_whatsapp(tx: Tx, clock: Clock, text: object, *, person_id: str | None = None) -> Touch | None:
    """The first known reference code in an inbound chat message, or None when it carries none.
    The message text is read here and dropped."""
    for candidate in code_candidates(text):
        source = find_code(tx, candidate)
        if source is not None:
            return _touch(tx, clock, TouchKind.WHATSAPP_CODE, source, person_id=person_id)
    return None


def capture_form(tx: Tx, clock: Clock, fields: Mapping[str, object], *, person_id: str | None = None) -> list[Touch]:
    """A form submission. Always one `form` touch (with no source when neither the hidden ref nor a
    typed code is known, so the lead still lands and counts as unsourced), plus a `self_report`
    touch when the answer is one of the fixed options."""
    source = find_slug(tx, fields.get(REF_PARAM)) or find_code(tx, fields.get(CODE_FIELD))
    out = [_touch(tx, clock, TouchKind.FORM, source, person_id=person_id)]
    answer = normalise_answer(fields.get(HOW_HEARD_FIELD))
    if answer is not None:
        out.append(_touch(tx, clock, TouchKind.SELF_REPORT, None, person_id=person_id, self_report=answer))
    return out


def capture_call(tx: Tx, clock: Clock, code: object, *, person_id: str | None = None) -> Touch:
    """A phone call. The seller types the code the caller read out, if any."""
    return _touch(tx, clock, TouchKind.CALL, find_code(tx, code), person_id=person_id)


def capture_walk_in(tx: Tx, clock: Clock, code: object, *, person_id: str | None = None) -> Touch:
    """A walk-in who mentions or shows an offer code."""
    return _touch(tx, clock, TouchKind.WALK_IN_CODE, find_code(tx, code), person_id=person_id)


def capture_self_report(tx: Tx, clock: Clock, answer: object, *, person_id: str | None = None) -> Touch | None:
    key = normalise_answer(answer)
    if key is None:
        return None
    return _touch(tx, clock, TouchKind.SELF_REPORT, None, person_id=person_id, self_report=key)


__all__ = ["CODE_FIELD", "HOW_HEARD_FIELD", "Source", "capture_call", "capture_form", "capture_self_report",
           "capture_walk_in", "capture_whatsapp", "find_code", "find_slug", "form_fields", "record_click"]
