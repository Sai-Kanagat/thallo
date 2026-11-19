"""The week's three questions and the shot list, per play.

The defaults come from the plays draft (`docs/plays/plays_draft.json`, voice_questions_examples
and shot_list_examples). English only for now: Italian and Tamil wording waits for Sai. A
founder or operator can rewrite a question for the week before anyone answers it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

QUESTIONS_PER_WEEK = 3


@dataclass(frozen=True)
class Shot:
    text: str
    people_in_shot: bool


QUESTIONS: dict[str, tuple[str, str, str]] = {
    "A": ("Which vehicle left the showroom this week, and what will the owner use it for?",
          "What did a customer ask this week that you had to explain twice?",
          "What is in stock right now that you would recommend, and why?"),
    "B": ("What question did a prospect or trial user ask this week that deserves a full answer?",
          "Which result did a customer get recently that you can describe with numbers we can verify?",
          "What do people get wrong when they compare tools in this category?"),
}
GENERIC: tuple[str, str, str] = (
    "What did a customer ask this week that you had to explain twice?",
    "What happened this week that you would tell a friend about the business?",
    "What is one thing you would like more people to know about what you sell?",
)

SHOTS: dict[str, tuple[Shot, ...]] = {
    "A": (Shot("A 20-second clip of today's delivery, with the customer's consent", True),
          Shot("Three photos of the model that arrived this week", False)),
    "B": (Shot("A 60-second screen recording of one task solved", False),
          Shot("One chart from a real, consented customer result", False)),
}
GENERIC_SHOTS: tuple[Shot, ...] = (
    Shot("Three photos of the work you did this week", False),
    Shot("A short clip of the place where you work", False),
)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def questions_for(play_code: str | None) -> tuple[str, str, str]:
    return QUESTIONS.get(play_code or "", GENERIC)


def shots_for(play_code: str | None) -> tuple[Shot, ...]:
    return SHOTS.get(play_code or "", GENERIC_SHOTS)


__all__ = ["GENERIC", "GENERIC_SHOTS", "QUESTIONS", "QUESTIONS_PER_WEEK", "SHOTS", "Shot", "monday",
           "questions_for", "shots_for"]
