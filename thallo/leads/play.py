"""The lead part of a play: fixed qualification questions, the fit rule table, heat weights, routes.

Loaded from `docs/plays/plays_draft.json` (WP-15 owns the play library; until it merges this reads
the draft file). Every number in it is a starting default, not a benchmark.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PLAYS_FILE = Path(__file__).resolve().parents[2] / "docs" / "plays" / "plays_draft.json"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Question(_Frozen):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    ask: str
    type: Literal["choice", "choice_from_catalogue", "text"]
    options: tuple[str, ...] = ()
    only_if: dict[str, str] = Field(default_factory=dict)

    def applies(self, answers: dict[str, Any]) -> bool:
        return all(answers.get(k) == v for k, v in self.only_if.items())


class FitRule(_Frozen):
    when: dict[str, str] = Field(alias="if")
    add: int


class RouteRule(_Frozen):
    fit_min: int | None = None
    fit_max: int | None = None
    heat_min: float | None = None


class LeadPlay(_Frozen):
    code: str
    name: str
    qualification_questions: tuple[Question, ...]
    fit_rules: tuple[FitRule, ...]
    fit_base: int = 0
    heat_actions: dict[str, int]
    routes: dict[Literal["hot", "warm", "low_fit"], RouteRule]

    def questions_for(self, answers: dict[str, Any]) -> list[Question]:
        """The questions that apply now (an `only_if` question appears once its condition holds)."""
        return [q for q in self.qualification_questions if q.applies(answers)]

    def question(self, key: str) -> Question | None:
        return next((q for q in self.qualification_questions if q.key == key), None)


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, LeadPlay]:
    data = json.loads(Path(path).read_text())
    return {p["code"]: LeadPlay.model_validate(p) for p in data["plays"]}


def load_play(code: str, path: Path = PLAYS_FILE) -> LeadPlay:
    plays = _load(str(path))
    if code not in plays:
        raise KeyError(f"no play {code!r}")
    return plays[code]


def play_codes(path: Path = PLAYS_FILE) -> list[str]:
    return sorted(_load(str(path)))


__all__ = ["FitRule", "LeadPlay", "Question", "RouteRule", "load_play", "play_codes"]
