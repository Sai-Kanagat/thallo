"""What a demo is made of. `demo_seed.py` turns one `Demo` into a database; `demo_sai.py` builds Sai's.

Plain data only: no database, no network, no clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from thallo.contracts.enums import Channel, ClaimKind, FunnelStage, SourceKind
from thallo.contracts.states import PlanSlotState


@dataclass(frozen=True)
class SourceSpec:
    key: str
    title: str
    kind: SourceKind = SourceKind.DOCUMENT
    uri: str | None = None
    captured_at: datetime | None = None  # None: when the seed runs


@dataclass(frozen=True)
class ClaimSpec:
    code: str
    kind: ClaimKind
    value: str
    unit: str | None
    source: str  # SourceSpec.key
    evidence: str  # the text whose sha256 is stored as the proof
    span: dict | None = None
    verified: bool = True  # False: stays a candidate, so no piece can use it
    expires_days: int | None = None


@dataclass(frozen=True)
class Piece:
    key: str
    drafts: tuple[str, ...]  # what the scripted writer says, one per attempt, in order
    template: str
    slots: dict[str, str]  # image slots; `{{C-0012}}` takes the claim's own rendered wording
    alt: str
    channel: Channel = Channel.INSTAGRAM
    candidates: int | None = None  # None: one per draft
    allowed: tuple[str, ...] | None = None  # claim codes the brief may use; None: every claim
    sources: tuple[str, ...] = ()  # source spans the writer may use; empty: the demo's default
    blocked_draft: bool = False  # no candidate compiles; the draft is shown with its FAIL, never approvable


@dataclass(frozen=True)
class Pillar:
    name: str
    stage: FunnelStage
    share: str


@dataclass(frozen=True)
class Slot:
    day: int
    channel: Channel
    stage: FunnelStage
    state: PlanSlotState
    piece: str | None = None
    pillar: int = 0


@dataclass(frozen=True)
class Demo:
    key: str
    tenant_name: str
    slug: str
    person: str  # the signed-in demo user's display name
    sources: tuple[SourceSpec, ...]
    claims: tuple[ClaimSpec, ...]
    pieces: tuple[Piece, ...]
    pillars: tuple[Pillar, ...]
    plan: tuple[Slot, ...]
    default_sources: tuple[str, ...]
    audience: str
    job: str
    call_to_action: str
    brand_dir: Path | None = None  # copied to <brand root>/<tenant>/brand; None: the repository's demo brand
    voice_rules: tuple[tuple[str, str, str], ...] = ()  # (do or dont, rule, evidence)
    extra_slop: tuple[str, ...] = ()
