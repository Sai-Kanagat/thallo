"""Approval rate, never alone (QUALITY_BAR 4 and 9). The only way to get an approval rate from
this package is `ApprovalWithOutcomes`, which cannot be built without one row per piece carrying
its outcome slot. Outcome per piece is measured elsewhere (WP-18); a piece without one shows
"outcome missing", never zero.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PieceOutcome:
    variant_id: str
    approved_first_pass: bool
    outcome: str | None  # e.g. "3 enquiries, 1 booking", written by measurement; None when not measured


@dataclass(frozen=True)
class ApprovalWithOutcomes:
    pieces: tuple[PieceOutcome, ...]

    def __init__(self, pieces: Sequence[PieceOutcome]) -> None:
        if not pieces:
            raise ValueError("an approval rate needs the pieces and their outcomes beside it")
        object.__setattr__(self, "pieces", tuple(pieces))

    @property
    def approval_rate(self) -> float:
        return sum(p.approved_first_pass for p in self.pieces) / len(self.pieces)

    def lines(self) -> list[str]:
        head = f"First-pass approval {self.approval_rate:.0%} of {len(self.pieces)} piece(s), with outcome per piece:"
        return [head] + [f"{p.variant_id}: {'approved' if p.approved_first_pass else 'edited'}, "
                         f"{p.outcome if p.outcome is not None else 'outcome missing'}" for p in self.pieces]


__all__ = ["ApprovalWithOutcomes", "PieceOutcome"]
