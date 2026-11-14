"""One normalised metric store, keyed by brand, channel, item and day offset."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Metric:
    brand: str
    channel: str
    item_id: str
    day: int
    name: str
    value: float
    pulled_at: datetime


@dataclass
class MetricStore:
    rows: list[Metric] = field(default_factory=list)

    def add(self, brand: str, channel: str, item_id: str, day: int,
            values: dict[str, float], now: datetime) -> None:
        self.rows += [Metric(brand, channel, item_id, day, k, v, now) for k, v in values.items()]

    def latest(self, item_id: str) -> dict[str, float]:
        rows = sorted((r for r in self.rows if r.item_id == item_id), key=lambda r: r.day)
        return {r.name: r.value for r in rows}
