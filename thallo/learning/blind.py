"""Blind comparison (QUALITY_BAR 8). Thallo pieces beside human-agency pieces for the same briefs,
scored by people who do not know which is which. Time from a Clock.

- Pieces are shuffled with a seeded generator and shown under opaque refs drawn from a secret salt,
  so neither the ref nor the position says the source. `presented()` carries no source field.
- A rater scores every dimension (brand fit, craft, specificity, would post), 1 to 5, once per
  piece. A score cannot be changed once given.
- `reveal(rater)` refuses until that rater has scored every piece. Only then are sources shown, and
  only alongside that rater's frozen scores.
- `verdict()` needs every listed rater finished. Thallo is "at least level" when its mean is not
  below the agency's on any dimension.
"""
from __future__ import annotations

import hashlib
import hmac
import random
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import mean

from thallo.contracts.clock import Clock
from thallo.contracts.ids import new_id
from thallo.learning.records import BlindItem, BlindScore, BlindSource


class Dimension(StrEnum):
    BRAND_FIT = "brand_fit"
    CRAFT = "craft"
    SPECIFICITY = "specificity"
    WOULD_POST = "would_post"


class BlindError(PermissionError):
    pass


@dataclass(frozen=True)
class Piece:
    source: BlindSource
    brief_ref: str
    text: str


@dataclass(frozen=True)
class Presented:
    """What a rater sees. Deliberately no source."""

    ref: str
    brief_ref: str
    text: str


@dataclass(frozen=True)
class Verdict:
    means: dict[BlindSource, dict[Dimension, float]]
    raters: int
    at_least_level: bool
    lines: tuple[str, ...]


class BlindSession:
    def __init__(self, *, tenant_id: str, pieces: Sequence[Piece], clock: Clock, seed: int | None = None,
                 salt: bytes | None = None) -> None:
        sources = {p.source for p in pieces}
        if sources != set(BlindSource):
            raise ValueError("a blind comparison needs pieces from both sources")
        self.tenant_id = tenant_id
        self.clock = clock
        self.id = new_id(clock)
        salt = salt if salt is not None else secrets.token_bytes(32)
        order = list(pieces)
        random.Random(seed if seed is not None else secrets.randbits(64)).shuffle(order)  # noqa: S311 - order only; refs come from the secret salt
        self.items: list[BlindItem] = []
        for pos, p in enumerate(order):
            ref = hmac.new(salt, f"{self.id}:{pos}".encode(), hashlib.sha256).hexdigest()[:16]
            self.items.append(BlindItem(id=new_id(clock), tenant_id=tenant_id, created_at=clock.now(),
                                        session_id=self.id, opaque_ref=ref, position=pos, source=p.source,
                                        brief_ref=p.brief_ref, text=p.text))
        self._by_ref = {i.opaque_ref: i for i in self.items}
        self.scores: dict[tuple[str, str], BlindScore] = {}

    def presented(self) -> list[Presented]:
        return [Presented(i.opaque_ref, i.brief_ref, i.text) for i in self.items]

    def score(self, *, rater_id: str, ref: str, scores: dict[Dimension, int]) -> BlindScore:
        item = self._by_ref.get(ref)
        if item is None:
            raise BlindError("no such piece in this session")
        if (rater_id, item.id) in self.scores:
            raise BlindError("this piece is already scored by this rater; scores are final")
        if set(scores) != set(Dimension):
            raise BlindError("score every dimension: brand fit, craft, specificity, would post")
        row = BlindScore(id=new_id(self.clock), tenant_id=self.tenant_id, created_at=self.clock.now(),
                         session_id=self.id, item_id=item.id, rater_id=rater_id,
                         **{d.value: scores[d] for d in Dimension}, scored_at=self.clock.now())
        self.scores[(rater_id, item.id)] = row
        return row

    def finished(self, rater_id: str) -> bool:
        return all((rater_id, i.id) in self.scores for i in self.items)

    def reveal(self, rater_id: str) -> list[tuple[Presented, BlindSource, BlindScore]]:
        if not self.finished(rater_id):
            left = sum(1 for i in self.items if (rater_id, i.id) not in self.scores)
            raise BlindError(f"sources stay hidden until every piece is scored: {left} left")
        return [(Presented(i.opaque_ref, i.brief_ref, i.text), i.source, self.scores[(rater_id, i.id)])
                for i in self.items]

    def verdict(self, raters: Sequence[str]) -> Verdict:
        if not raters:
            raise BlindError("name the raters")
        unfinished = [r for r in raters if not self.finished(r)]
        if unfinished:
            raise BlindError(f"{len(unfinished)} rater(s) have not finished scoring")
        source_of = {i.id: i.source for i in self.items}
        means: dict[BlindSource, dict[Dimension, float]] = {}
        for src in BlindSource:
            rows = [s for (r, iid), s in self.scores.items() if r in raters and source_of[iid] is src]
            means[src] = {d: round(mean(getattr(s, d.value) for s in rows), 2) for d in Dimension}
        level = all(means[BlindSource.THALLO][d] >= means[BlindSource.AGENCY][d] for d in Dimension)
        lines = tuple(f"{d.value}: Thallo {means[BlindSource.THALLO][d]}, agency {means[BlindSource.AGENCY][d]}"
                      for d in Dimension)
        return Verdict(means, len(raters), level, lines)


__all__ = ["BlindError", "BlindSession", "Dimension", "Piece", "Presented", "Verdict"]
