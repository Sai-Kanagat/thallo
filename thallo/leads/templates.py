"""The acknowledgement template: approved once by hash, slots filled only from the lead's own words.

- `template_hash` covers kind, channel, language, body and slot sources. Any change is a new hash,
  so an earlier approval stops matching.
- `promise_problems` refuses wording that promises anything (prices, numbers, discounts,
  guarantees, dates). An acknowledgement says "we got it, a person will reply".
- `render` fills `{{slot}}` only from sources the template names: `person.display_name` or
  `answer.<key>`, i.e. what the lead typed. Values are untrusted: control characters and links are
  removed, length is capped, and a slot whose value is unusable falls back to an empty string.
- `live_approval` finds a template approval that is current: subject TEMPLATE, same hash, not
  voided, not expired, by a founder, approver or operator.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from thallo.contracts.enums import ApprovalSubject, UserRole
from thallo.contracts.models import Approval, AppUser, MessageTemplate

SLOT = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,39})\s*\}\}")
SOURCE = re.compile(r"^(person\.display_name|answer\.[a-z][a-z0-9_]{0,39})$")
MAX_SLOT_CHARS = 40
APPROVER_ROLES = frozenset({UserRole.FOUNDER, UserRole.APPROVER, UserRole.OPERATOR})

_PROMISE = re.compile(
    r"\d|[%€$£]|\b(guarantee\w*|promise\w*|free|discount\w*|offer|deal|price\w*|cheap\w*|best|"
    r"tomorrow|today|within|garanti\w*|promess\w*|gratis|gratuit\w*|sconto|sconti|offerta|prezz\w*|"
    r"domani|oggi|entro)\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")
_LINKISH = re.compile(r"(https?://|www\.|\S+\.[a-z]{2,}(/|\b)|@|<|>|\{|\})", re.IGNORECASE)


def template_hash(t: MessageTemplate) -> str:
    form = {"kind": t.kind.value, "channel": t.channel.value, "language": t.language.value, "body": t.body,
            "slot_sources": dict(sorted(t.slot_sources.items()))}
    return hashlib.sha256(json.dumps(form, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def promise_problems(t: MessageTemplate) -> list[str]:
    out: list[str] = []
    text = SLOT.sub("", t.body)
    for m in _PROMISE.finditer(text):
        out.append(f"promises something: {m.group(0)!r}")
    slots = set(SLOT.findall(t.body))
    for s in slots - set(t.slot_sources):
        out.append(f"slot {s!r} has no source")
    for s, src in t.slot_sources.items():
        if not SOURCE.match(src):
            out.append(f"slot {s!r}: source {src!r} is not the lead's own words")
    if t.content_sha256 != template_hash(t):
        out.append("content_sha256 does not match the template")
    return out


def clean_slot(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub(" ", value)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or _LINKISH.search(text):
        return ""
    return text[:MAX_SLOT_CHARS].strip()


def render(t: MessageTemplate, *, display_name: str | None, answers: dict[str, Any]) -> str:
    def value(slot: str) -> str:
        src = t.slot_sources.get(slot, "")
        if src == "person.display_name":
            return clean_slot((display_name or "").split(" ")[0])
        if src.startswith("answer."):
            return clean_slot(answers.get(src.removeprefix("answer.")))
        return ""

    out = SLOT.sub(lambda m: value(m.group(1)), t.body)
    return re.sub(r"[ \t]+([,.!?])", r"\1", re.sub(r"[ \t]{2,}", " ", out)).strip()


def live_approval(t: MessageTemplate, approvals: Iterable[Approval], users: dict[str, AppUser],
                  now: datetime) -> Approval | None:
    want = template_hash(t)
    if t.content_sha256 != want:
        return None
    for a in sorted(approvals, key=lambda a: a.approved_at, reverse=True):
        u = users.get(a.approver_id)
        if (a.subject_kind is ApprovalSubject.TEMPLATE and a.subject_id == t.id and a.content_hash == want
                and a.voided_at is None and a.expires_at > now and a.approved_at <= now
                and u is not None and u.role in APPROVER_ROLES):
            return a
    return None


__all__ = ["clean_slot", "live_approval", "promise_problems", "render", "template_hash"]
