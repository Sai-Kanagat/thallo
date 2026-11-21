"""Reviews and questions in, as untrusted data (plain code, no network).

Two inputs, both files a person supplies:
- `parse_reviews`: the JSON shape of the Business Profile API `accounts.locations.reviews.list`
  reply (`{"reviews": [{reviewId, reviewer: {displayName, isAnonymous}, starRating, comment,
  createTime, reviewReply}]}`, read from developers.google.com 20 Nov 2026). The engine never calls
  that API itself here; a person or a later connection saves the reply as a file.
- `parse_questions`: a plain list `[{"id", "author", "text", "created_at"}]`. Whether Google still
  offers a questions API was not verified, so questions come in by hand only.

Every text field is kept exactly as given and travels only as `untrusted_data` downstream.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
LOW_STARS = 2  # a review at or under this needs the person to say they read it before approving
MAX_BODY = 4096
MAX_ITEMS = 1000
_LAST = datetime.max.replace(tzinfo=UTC)


class InboxError(ValueError):
    pass


@dataclass(frozen=True)
class InboxItem:
    kind: str  # "review" or "question"
    external_id: str
    body: str
    rating: int | None = None
    author: str | None = None
    written_at: datetime | None = None
    already_answered: bool = False
    source: str = "api_export"

    @property
    def is_low(self) -> bool:
        return self.kind == "review" and self.rating is not None and self.rating <= LOW_STARS


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _load(raw: str | bytes | Any) -> Any:
    if isinstance(raw, str | bytes):
        try:
            return json.loads(raw)
        except ValueError:
            raise InboxError("the file is not JSON") from None
    return raw


def _ident(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 300:
        raise InboxError("an item has no usable id")
    return value


def parse_reviews(raw: str | bytes | Any) -> list[InboxItem]:
    data = _load(raw)
    reviews = data.get("reviews") if isinstance(data, dict) else None
    if not isinstance(reviews, list):
        raise InboxError("expected an object with a 'reviews' list")
    if len(reviews) > MAX_ITEMS:
        raise InboxError(f"more than {MAX_ITEMS} reviews in one file")
    out: list[InboxItem] = []
    for r in reviews:
        if not isinstance(r, dict):
            raise InboxError("a review is not an object")
        stars = STARS.get(r.get("starRating"))  # type: ignore[arg-type]
        if stars is None:
            raise InboxError("a review has no star rating from ONE to FIVE")
        who = r.get("reviewer") if isinstance(r.get("reviewer"), dict) else {}
        author = None if who.get("isAnonymous") else who.get("displayName")
        body = r.get("comment") if isinstance(r.get("comment"), str) else ""
        reply = r.get("reviewReply")
        out.append(InboxItem(kind="review", external_id=_ident(r.get("reviewId")), body=body[:MAX_BODY],
                             rating=stars, author=author if isinstance(author, str) else None,
                             written_at=_when(r.get("createTime")),
                             already_answered=isinstance(reply, dict) and bool(reply.get("comment"))))
    return out


def parse_questions(raw: str | bytes | Any) -> list[InboxItem]:
    data = _load(raw)
    if not isinstance(data, list):
        raise InboxError("expected a list of questions")
    if len(data) > MAX_ITEMS:
        raise InboxError(f"more than {MAX_ITEMS} questions in one file")
    out: list[InboxItem] = []
    for q in data:
        if not isinstance(q, dict) or not isinstance(q.get("text"), str) or not q["text"].strip():
            raise InboxError("a question has no text")
        author = q.get("author")
        out.append(InboxItem(kind="question", external_id=_ident(q.get("id")), body=q["text"][:MAX_BODY],
                             author=author if isinstance(author, str) else None,
                             written_at=_when(q.get("created_at")), source="manual"))
    return out


def needs_answer(items: list[InboxItem]) -> list[InboxItem]:
    """Unanswered items, low ratings first, then oldest first."""
    open_items = [i for i in items if not i.already_answered]
    return sorted(open_items, key=lambda i: (not i.is_low, i.written_at is None, i.written_at or _LAST))
