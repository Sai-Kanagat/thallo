"""Taste ledger: repeated human edits become proposed rules, accepted by a person."""
from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field

WORDS = re.compile(r"\S+")


def removed_phrases(draft: str, final: str) -> list[str]:
    a, b = WORDS.findall(draft.lower()), WORDS.findall(final.lower())
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    return [" ".join(a[i1:i2]).strip(".,!?:;") for tag, i1, i2, _, _ in matcher.get_opcodes()
            if tag in ("delete", "replace")]


def edit_ratio(draft: str, final: str) -> float:
    return 1 - difflib.SequenceMatcher(a=draft, b=final, autojunk=False).ratio()


@dataclass
class TasteLedger:
    threshold: int = 2
    rules: list[str] = field(default_factory=list)
    version: int = 0
    declined: set[str] = field(default_factory=set)
    removals: Counter = field(default_factory=Counter)
    graveyard: list[tuple[str, str]] = field(default_factory=list)
    edit_ratios: list[float] = field(default_factory=list)

    def record_edit(self, draft: str, final: str) -> None:
        self.edit_ratios.append(edit_ratio(draft, final))
        self.removals.update(set(p for p in removed_phrases(draft, final) if p))

    def reject(self, idea: str, reason: str) -> None:
        self.graveyard.append((idea, reason))

    def proposals(self) -> list[str]:
        return [f'Avoid "{phrase}"' for phrase, n in self.removals.items()
                if n >= self.threshold and f'Avoid "{phrase}"' not in self.rules
                and f'Avoid "{phrase}"' not in self.declined]

    def accept(self, rule: str) -> None:
        self.rules.append(rule)
        self.version += 1

    def decline(self, rule: str) -> None:
        self.declined.add(rule)
