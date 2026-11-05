"""Foresight ledger: sealed, hash-chained forecasts, public hash, private payload."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

from cortex_operator.contracts import sha256

GENESIS = "0" * 64


@dataclass(frozen=True)
class Entry:
    seq: int
    at: str
    previous: str
    digest: str


class ForecastLedger:
    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.sealed: dict[int, dict] = {}
        self.scores: dict[int, dict] = {}

    def commit(self, subject: str, forecast: dict, baseline: dict, now: datetime) -> int:
        seq = len(self.entries) + 1
        previous = self.entries[-1].digest if self.entries else GENESIS
        payload = {"seq": seq, "subject": subject, "forecast": forecast,
                   "baseline": baseline, "salt": secrets.token_hex(32)}
        at = now.isoformat()
        self.entries.append(Entry(seq, at, previous, sha256([payload, previous, at])))
        self.sealed[seq] = payload
        return seq

    def verify_chain(self) -> bool:
        previous = GENESIS
        for entry in self.entries:
            payload = self.sealed.get(entry.seq)
            if (payload is None or entry.previous != previous
                    or sha256([payload, previous, entry.at]) != entry.digest):
                return False
            previous = entry.digest
        return True

    def score(self, seq: int, actual: dict[str, float]) -> dict:
        """Absolute error per metric for forecast and baseline. Lower wins."""
        payload = self.sealed[seq]
        result = {}
        for metric, value in actual.items():
            if metric in payload["forecast"] and metric in payload["baseline"]:
                result[metric] = {
                    "actual": value,
                    "forecast_error": abs(payload["forecast"][metric] - value),
                    "baseline_error": abs(payload["baseline"][metric] - value),
                }
        self.scores[seq] = result
        return result
