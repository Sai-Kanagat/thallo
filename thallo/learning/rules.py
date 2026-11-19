"""Typed voice rules. The legacy taste ledger (`thallo/taste.py`) kept rules as strings; here each
rule is a typed object that can say, in code, whether a text breaks it. A rule that cannot be
checked in code is not a rule.

- AvoidPhrase: the phrase must not appear (from repeated cuts).
- PreferWording: `avoid` must not appear; `use` is what the human wrote instead (from swaps).
- MaxExclamations: at most `limit` exclamation marks (from repeated tone edits that removed them).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def key(self) -> str:
        """Stable identity: two proposals with the same key are the same rule."""
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    def broken_by(self, text: str) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


def _contains(text: str, phrase: str) -> bool:
    return re.search(r"(?<![\w'])" + re.escape(phrase) + r"(?![\w'])", text, re.I) is not None


class AvoidPhrase(_Rule):
    type: Literal["avoid_phrase"] = "avoid_phrase"
    phrase: str = Field(min_length=1)

    def broken_by(self, text: str) -> bool:
        return _contains(text, self.phrase)

    def describe(self) -> str:
        return f'Avoid "{self.phrase}"'


class PreferWording(_Rule):
    type: Literal["prefer_wording"] = "prefer_wording"
    avoid: str = Field(min_length=1)
    use: str = Field(min_length=1)

    def broken_by(self, text: str) -> bool:
        return _contains(text, self.avoid)

    def describe(self) -> str:
        return f'Write "{self.use}", not "{self.avoid}"'


class MaxExclamations(_Rule):
    type: Literal["max_exclamations"] = "max_exclamations"
    limit: int = Field(ge=0)

    def broken_by(self, text: str) -> bool:
        return text.count("!") > self.limit

    def describe(self) -> str:
        return f"At most {self.limit} exclamation mark(s)"


Rule = Annotated[AvoidPhrase | PreferWording | MaxExclamations, Field(discriminator="type")]
_ADAPTER: TypeAdapter[Rule] = TypeAdapter(Rule)


def parse_rule(data: dict) -> AvoidPhrase | PreferWording | MaxExclamations:
    return _ADAPTER.validate_python(data)


__all__ = ["AvoidPhrase", "MaxExclamations", "PreferWording", "Rule", "parse_rule"]
