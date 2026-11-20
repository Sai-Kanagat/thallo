"""The engagement inbox (BUILD_PLAN module 13): comments and messages brought to a person with a
suggested reply. A person sends, in the platform, by copying the suggestion or opening the item.

There is no send path in this package, by design: nothing here imports a messaging or publishing
connection, builds a send or publish request, or calls a platform. `tests/wp23_engage` checks that.

- Every comment is untrusted data. It is stored cleaned and capped, triaged by plain rules
  (`triage.py`), and reaches a model only as quoted data (`suggest.py`).
- Abuse and crisis items are flagged to a person first: no suggestion is drafted until a verified
  live person has looked at the item (`acknowledge`).
- A comment that is a lead is handed to the lead desk through its interface (`Desk.intake` with a
  `TouchEvent`) by a person. The inbox never records consent and never drafts a message to the lead;
  the desk's own consent rules (bound to the exact recipient) and its one approved send path decide
  whatever happens next.

Works on a tenant transaction with `get`, `list_all`, `insert`, `update`; row-level security scopes
every call to one tenant.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from thallo.contracts.actors import Actor, HumanActor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Channel, HumanOnlyAction, Language, TouchKind
from thallo.contracts.ids import new_id
from thallo.engage.records import FLAGGED, Category, EngageItem, ItemState, Source
from thallo.engage.suggest import PlainSuggester, Suggester
from thallo.engage.triage import triage

MAX_TEXT = 4000
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")
_PERMALINK = re.compile(r"^https://[a-z0-9.-]{1,190}(/[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]{0,1500})?$")
CHANNELS = frozenset({Channel.INSTAGRAM, Channel.FACEBOOK, Channel.LINKEDIN_COMPANY, Channel.LINKEDIN_PERSONAL,
                      Channel.GOOGLE_BUSINESS_PROFILE, Channel.YOUTUBE, Channel.WHATSAPP, Channel.EMAIL,
                      Channel.WEBSITE})
CLOSED = frozenset({ItemState.HANDLED, ItemState.HANDED_TO_LEADS, ItemState.DISMISSED})


class InboxError(Exception):
    """A refusal a person can read. `code` is stable for the API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Incoming:
    """One comment or message as a feed or a person delivers it. Every text field is untrusted."""

    channel: Channel
    source: Source
    external_id: str
    text: str
    received_at: datetime
    author: str | None = None
    permalink: str | None = None


@runtime_checkable
class EngagementFeed(Protocol):
    """Where comments come from. No real feed exists yet (see the package report); manual entry and
    `FakeFeed` cover tests and the demo. A feed only reads; it never answers."""

    def pending(self, tenant_id: str) -> Iterable[Incoming]: ...

    def taken(self, tenant_id: str, item: Incoming) -> None: ...


class FakeFeed:
    def __init__(self) -> None:
        self._queue: dict[str, list[Incoming]] = {}

    def push(self, tenant_id: str, item: Incoming) -> None:
        self._queue.setdefault(tenant_id, []).append(item)

    def pending(self, tenant_id: str) -> list[Incoming]:
        return list(self._queue.get(tenant_id, []))

    def taken(self, tenant_id: str, item: Incoming) -> None:
        self._queue.get(tenant_id, []).remove(item)


def clean_text(value: str | None, cap: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = _CTRL.sub(" ", value)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:cap] or None


def safe_permalink(link: str | None) -> str | None:
    """Only a plain https link opens the item in its platform; anything else is dropped."""
    if link and _PERMALINK.match(link.strip()) and not re.search(r"[\s\"'<>\\]", link.strip()):
        return link.strip()
    return None


@dataclass
class Inbox:
    tx: Any
    tenant_id: str
    clock: Clock
    language: Language = Language.EN
    suggester: Suggester = field(default_factory=PlainSuggester)

    # ------------------------------------------------------------------ in
    def ingest(self, item: Incoming) -> EngageItem:
        """Store, triage and (unless flagged) suggest. The same channel and external id twice is
        one item: the second delivery returns the first row unchanged."""
        if item.channel not in CHANNELS:
            raise InboxError("bad_channel", f"{item.channel.value} is not an engagement channel")
        ext = clean_text(item.external_id, 200)
        text = clean_text(item.text, MAX_TEXT)
        if not ext or not text:
            raise InboxError("bad_item", "a comment needs an id and some text")
        for row in self.tx.list_all(EngageItem, channel=item.channel, external_id=ext):
            return row
        verdict = triage(text)
        now = self.clock.now()
        row = EngageItem(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id, channel=item.channel,
                         source=item.source, external_id=ext, author=clean_text(item.author, 80), text=text,
                         permalink=safe_permalink(item.permalink), received_at=item.received_at,
                         category=verdict.category, reasons=list(verdict.reasons),
                         state=ItemState.FLAGGED if verdict.category in FLAGGED else ItemState.OPEN,
                         updated_at=now)
        if row.state is ItemState.OPEN:
            row = self._with_suggestion(row)
        self.tx.insert(row)
        return row

    def pull(self, feed: EngagementFeed) -> list[EngageItem]:
        out = []
        for item in list(feed.pending(self.tenant_id)):
            out.append(self.ingest(item))
            feed.taken(self.tenant_id, item)
        return out

    def _with_suggestion(self, row: EngageItem) -> EngageItem:
        s = self.suggester.suggest(tenant_id=self.tenant_id, language=self.language, category=row.category,
                                   text=row.text, idempotency_key=f"engage:{row.id}")
        if s is None:
            return row
        return row.model_copy(update={"suggested_reply": s.text, "suggestion_model": s.model_id,
                                      "prompt_version": s.prompt_version})

    # ------------------------------------------------------------------ reading
    def item(self, item_id: str) -> EngageItem:
        row = self.tx.get(EngageItem, item_id)
        if row is None:
            raise InboxError("not_found", "no such item")
        return row

    def items(self, *, include_closed: bool = False) -> list[EngageItem]:
        rows = [r for r in self.tx.list_all(EngageItem) if include_closed or r.state not in CLOSED]
        rank = {ItemState.FLAGGED: 0, ItemState.OPEN: 1}
        crank = {Category.CRISIS: 0, Category.ABUSE: 1, Category.LEAD: 2, Category.COMPLAINT: 3}
        return sorted(rows, key=lambda r: (rank.get(r.state, 2), crank.get(r.category, 4),
                                           -r.received_at.timestamp()))

    # ------------------------------------------------------------------ a person acts
    def _human(self, actor: Actor, verifier: SessionVerifier) -> HumanActor:
        return require_human(actor, HumanOnlyAction.PUBLIC_REPLY, verifier=verifier, tenant_id=self.tenant_id)

    def _set(self, row: EngageItem, **fields: Any) -> EngageItem:
        fields["updated_at"] = self.clock.now()
        self.tx.update(EngageItem, row.id, **fields)
        return row.model_copy(update=fields)

    def acknowledge(self, item_id: str, *, actor: Actor, verifier: SessionVerifier) -> EngageItem:
        """A person has looked at a flagged item. Only then is a suggestion drafted, and the item
        keeps its category so the screen still shows what it was flagged for."""
        human = self._human(actor, verifier)
        row = self.item(item_id)
        if row.state is not ItemState.FLAGGED:
            raise InboxError("not_flagged", "only a flagged item is acknowledged")
        row = self._with_suggestion(row)
        return self._set(row, state=ItemState.OPEN, acknowledged_by=human.id, suggested_reply=row.suggested_reply,
                         suggestion_model=row.suggestion_model, prompt_version=row.prompt_version)

    def _open(self, item_id: str) -> EngageItem:
        row = self.item(item_id)
        if row.state is ItemState.FLAGGED:
            raise InboxError("flagged", "a person looks at a flagged item first")
        if row.state in CLOSED:
            raise InboxError("closed", "this item is already closed")
        return row

    def edit_suggestion(self, item_id: str, text: str) -> EngageItem:
        row = self._open(item_id)
        reply = clean_text(text, 1500)
        if not reply:
            raise InboxError("bad_reply", "the reply is empty")
        return self._set(row, suggested_reply=reply)

    def handled(self, item_id: str, *, actor: Actor, verifier: SessionVerifier) -> EngageItem:
        """The person replied in the platform themselves. Recorded, never sent from here."""
        human = self._human(actor, verifier)
        return self._set(self._open(item_id), state=ItemState.HANDLED, handled_by=human.id)

    def dismiss(self, item_id: str, *, actor: Actor, verifier: SessionVerifier) -> EngageItem:
        human = self._human(actor, verifier)
        row = self.item(item_id)
        if row.state in CLOSED:
            raise InboxError("closed", "this item is already closed")
        if row.state is ItemState.FLAGGED and row.acknowledged_by is None:
            row = self._set(row, acknowledged_by=human.id)
        return self._set(row, state=ItemState.DISMISSED, handled_by=human.id)

    def hand_to_leads(self, item_id: str, desk: Any, *, actor: Actor, verifier: SessionVerifier,
                      phone: str | None = None, email: str | None = None) -> EngageItem:
        """A person turns a comment into an enquiry on the lead desk. Phone or email come only from
        the person (who may have asked for them); the inbox never lifts them from the comment.
        The desk opens the lead as a self report, so it drafts no acknowledgement; consent stays
        the desk's decision, per exact recipient."""
        from thallo.leads.desk import DeskError
        from thallo.leads.touches import TouchEvent

        human = self._human(actor, verifier)
        row = self._open(item_id)
        if row.category in FLAGGED and row.acknowledged_by is None:
            raise InboxError("flagged", "a person looks at a flagged item first")
        ev = TouchEvent(kind=TouchKind.SELF_REPORT, occurred_at=row.received_at, display_name=row.author,
                        phone=phone, email=email,
                        self_report=f"{row.source.value} on {row.channel.value.replace('_', ' ')}")
        try:
            lead = desk.intake(ev)
        except DeskError as exc:
            raise InboxError(exc.code, str(exc)) from None
        return self._set(row, state=ItemState.HANDED_TO_LEADS, handled_by=human.id, lead_id=lead.id)


__all__ = ["CHANNELS", "EngagementFeed", "FakeFeed", "Inbox", "InboxError", "Incoming", "clean_text",
           "safe_permalink"]
