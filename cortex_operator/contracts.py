"""Shared types and interfaces. Everything else depends on this file."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol


class Status(str, Enum):
    IDEA = "idea"
    NEEDS_EVIDENCE = "needs_evidence"
    REVIEW = "review"
    APPROVED = "approved"
    ON_HOLD = "on_hold"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass
class Item:
    id: str
    brand: str
    channel: str
    brief: str
    publish_at: datetime
    status: Status = Status.IDEA
    text: str = ""
    media: list[str] = field(default_factory=list)
    url: str | None = None
    forecast_seq: int | None = None
    metrics_pulled: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Passport:
    item_id: str
    approver: str
    approved_at: datetime
    content_hash: str
    rule_version: int
    claim_ids: tuple[str, ...]


@dataclass
class BrandSettings:
    name: str
    channels: set[str]
    paused: bool = False
    weekly_spend_cap_eur: float = 0.0
    spent_this_week_eur: float = 0.0


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class Publisher(Protocol):
    def publish(self, key: str, item: Item) -> str: ...


class MetricsSource(Protocol):
    def pull(self, item: Item) -> dict[str, float]: ...


class Writer(Protocol):
    def draft(self, item: Item, rules: list[str]) -> str: ...


class Cockpit(Protocol):
    """Notion in production. Items in, status and notes out."""

    def items(self) -> list[Item]: ...
    def notify(self, item: Item, message: str) -> None: ...


def sha256(value: object) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def week_offsets(publish_at: datetime) -> dict[str, datetime]:
    """The weekly rhythm, for an item publishing on Monday."""
    day = publish_at.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "generate": day - timedelta(days=7),
        "hold": day - timedelta(days=4) + timedelta(hours=12),
        "schedule": day - timedelta(days=3),
    }
