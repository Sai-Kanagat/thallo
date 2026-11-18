"""Fixtures for WP-07. No keys, no network: the graders and the second reader run through the real
registry and the real Claude and Gemini providers, with scripted transports replaying replies shaped
like the hand-written fixtures in tests/wp06_text/fixtures.json. The vision grader runs on recorded
replies (`RecordedVision`): the contract has no request that carries image bytes yet."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import zlib
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from thallo.checks import Context, FinishedPiece, Settings, VisionReply
from thallo.connections import (
    ConnectionRegistry,
    InMemoryConnectionStore,
    InMemorySecretStore,
    KeyRing,
    ProviderRegistry,
    Vault,
)
from thallo.contracts.actors import HumanActor, SystemActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import (
    Capability,
    Channel,
    CheckVerdict,
    ClaimKind,
    ConnectionMode,
    Language,
    MediaKind,
    MediaProvenance,
    TextRole,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import Brief, Media, Tenant, Variant
from thallo.contracts.states import VariantState
from thallo.contracts.testing.samples import T, _id
from thallo.gate import VariantContent
from thallo.gate.canonical import MediaItem, build_snapshot
from thallo.gate.checks import content_types, review_checks
from thallo.media.lint import CHECKS as LINT_CHECKS
from thallo.media.lint import Finding as LintFinding
from thallo.media.lint import LintReport
from thallo.providers.text import claude, gemini
from thallo.providers.text.base import HttpReply
from thallo.truth import build, new_candidate, verify_claim
from thallo.truth.repository import InMemoryClaimRepository

FAKE_KEY = "fake-key-for-tests-only"  # not a real key; never sent anywhere
WRITER = "claude-sonnet-5-5"
READER = "gemini-3.1-flash-lite"
SOURCE = "Owner: we bake the sourdough at four every morning. A loaf costs twelve euros at the weekend."
CLEAN = "Our sourdough comes out of the oven before the shop opens. A loaf is {{C-0001}} this weekend."
PASSING_LINT = LintReport(findings=tuple(LintFinding(check=n, verdict=CheckVerdict.PASS) for n in LINT_CHECKS))
NO_FLAGS = json.dumps({"flags": []})
NO_UNSUPPORTED = json.dumps({"unsupported": []})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


# ---------------------------------------------------------------- images

def png(width: int, height: int, pixel=lambda x, y: (128, 128, 128)) -> bytes:
    """An 8-bit RGB PNG, filter 0 on every row, written with the standard library."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(pixel(x, y))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def checker(x: int, y: int) -> tuple[int, int, int]:
    """Sharp, mid-exposed detail: alternating blocks of light and dark."""
    v = 200 if ((x // 4) + (y // 4)) % 2 else 60
    return (v, v, v)


SHARP_PHOTO = png(1080, 1350, checker)
TEMPLATE_PNG = png(8, 8, lambda x, y: (10, 120, 200))


# ---------------------------------------------------------------- scripted vendors

class ClaudeScript:
    """Claude-shaped replies from a queue of texts (or callables taking the call)."""

    def __init__(self, *texts, output_tokens: int = 60, stop: str = "end_turn", model: str = WRITER) -> None:
        self.texts = list(texts)
        self.output_tokens, self.stop, self.model = output_tokens, stop, model
        self.calls = []

    def __call__(self, call):
        if call.method == "GET":
            return HttpReply(200, {"id": self.model})
        self.calls.append(call)
        text = self.texts.pop(0) if self.texts else NO_FLAGS
        return HttpReply(200, {"id": f"msg_{len(self.calls)}", "model": self.model, "stop_reason": self.stop,
                               "content": [{"type": "text", "text": text}],
                               "usage": {"input_tokens": 500, "output_tokens": self.output_tokens}})

    def system(self, i: int) -> str:
        return self.calls[i].body["system"]

    def user(self, i: int) -> str:
        return self.calls[i].body["messages"][0]["content"][0]["text"]


class GeminiScript:
    def __init__(self, *texts, model: str = READER) -> None:
        self.texts = list(texts)
        self.model = model
        self.calls = []

    def __call__(self, call):
        if call.method == "GET":
            return HttpReply(200, {"name": f"models/{READER}", "supportedGenerationMethods": ["generateContent"]})
        self.calls.append(call)
        text = self.texts.pop(0) if self.texts else NO_UNSUPPORTED
        return HttpReply(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                                               "finishReason": "STOP", "index": 0}],
                               "usageMetadata": {"promptTokenCount": 400, "candidatesTokenCount": 30,
                                                 "totalTokenCount": 430},
                               "modelVersion": self.model})


PRICES = {"input_usd_per_mtok": "2", "output_usd_per_mtok": "10"}


@dataclass
class Vendors:
    grader: ClaudeScript = field(default_factory=ClaudeScript)
    reader: GeminiScript = field(default_factory=GeminiScript)


def make_registry(clock, vault, vendors: Vendors, *, reader_provider: str = "gemini", reader_model: str = READER,
                  grader_prices=PRICES, tenant_id: str = T) -> ConnectionRegistry:
    providers = ProviderRegistry.with_discovered()
    providers.register(Capability.TEXT, "claude", ConnectionMode.HOSTED,
                       lambda ctx: claude.create(ctx, transport=vendors.grader), replace=True,
                       settings_schema=claude.ClaudeSettings)
    providers.register(Capability.TEXT, "gemini", ConnectionMode.HOSTED,
                       lambda ctx: gemini.create(ctx, transport=vendors.reader), replace=True,
                       settings_schema=gemini.GeminiSettings)
    reg = ConnectionRegistry(clock=clock, providers=providers, connections=InMemoryConnectionStore(), vault=vault,
                             environ={})
    reg.add(tenant_id, Capability.TEXT, "claude", role=TextRole.GRADE, model=WRITER, secret=FAKE_KEY,
            settings=dict(grader_prices))
    reg.add(tenant_id, Capability.TEXT, reader_provider, role=TextRole.SECOND_READ, model=reader_model, secret=FAKE_KEY,
            settings=dict(PRICES))
    return reg


# ---------------------------------------------------------------- the world

class Verifier:
    def is_live_human_session(self, actor):
        return actor.session_id == "live"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock) -> Vault:
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


@pytest.fixture
def tenant(clock):
    return Tenant(id=T, created_at=clock.now(), operator_id=_id(1), name="Bakery", slug="bakery",
                  default_language=Language.EN, region_code="IT", currency="EUR")


@pytest.fixture
def price(clock):
    human = HumanActor(id=_id(7), tenant_id=T, session_id="live")
    cand, _ = new_candidate(tenant_id=T, code="C-0001", kind=ClaimKind.PRICE, value="12", unit="EUR", clock=clock,
                            actor=SystemActor(id="extractor"), evidence="price list")
    claim, _ = verify_claim(cand, human, verifier=Verifier(), clock=clock, repo=InMemoryClaimRepository())
    return claim


@pytest.fixture
def brief(clock, price):
    return Brief(id=_id(30), created_at=clock.now(), tenant_id=T, idea_id=_id(31), audience="neighbours",
                 job_to_be_done="buy bread for the weekend", message="our sourdough is baked fresh every morning",
                 channels=[Channel.INSTAGRAM], allowed_claim_ids=[price.id], call_to_action="order a loaf",
                 language=Language.EN)


def NO_LINKS(url):  # noqa: N802 - the test repository's resolver: it knows no link
    return None


def media_item(clock, data: bytes, provenance: MediaProvenance, *, position: int = 0, mime: str = "image/png",
               kind: MediaKind = MediaKind.IMAGE) -> MediaItem:
    sha = hashlib.sha256(data).hexdigest()
    row = Media(id=new_id(clock), created_at=clock.now(), tenant_id=T, kind=kind, sha256=sha, mime=mime,
                size_bytes=len(data), storage_uri=f"mem://{sha[:8]}", provenance=provenance, alt_text="Bread")
    return MediaItem(media=row, position=position, bytes_sha256=sha)


@pytest.fixture
def make_piece(clock, price, brief):
    def _make(text: str = CLEAN, *, media: tuple[tuple[bytes, MediaProvenance], ...] = (), rendered: str | None = None,
              mime: str = "image/png", **kw) -> FinishedPiece:
        if rendered is None:
            try:
                rendered = build(text, tenant_id=T, language=Language.EN, claims={"C-0001": price}, clock=clock,
                                 default_language=Language.EN).filled.text
            except Exception:  # noqa: BLE001 - a piece that does not compile keeps its source text
                rendered = text
        variant = Variant(id=_id(50), created_at=clock.now(), tenant_id=T, bundle_id=_id(51),
                          channel=Channel.INSTAGRAM, language=Language.EN, state=VariantState.CHECKING, text=text,
                          rendered_text=rendered, task_type="post", model_id=WRITER)
        items = tuple(media_item(clock, data, prov, position=i, mime=mime) for i, (data, prov) in enumerate(media))
        content = VariantContent(variant=variant, media=items, claims=(price,) if "C-0001" in text else ())
        kw.setdefault("media_bytes", {i.bytes_sha256: data for i, (data, _) in zip(items, media, strict=True)})
        kw.setdefault("lint", {i.bytes_sha256: PASSING_LINT for i in items
                               if i.media.provenance is MediaProvenance.TEMPLATE_RENDER})
        kw.setdefault("sources", (SOURCE,))
        kw.setdefault("brief", brief)
        return FinishedPiece(content=content, **kw)
    return _make


@pytest.fixture
def vendors() -> Vendors:
    return Vendors()


@pytest.fixture
def make_ctx(clock, vault, tenant, price, vendors):
    def _make(*, registry=None, **kw) -> Context:
        reg = registry or make_registry(clock, vault, vendors)
        kw.setdefault("settings", Settings())
        return Context(tenant=tenant, claims={"C-0001": price}, clock=clock, resolver=NO_LINKS, non_links=(),
                       registry=reg, **kw)
    return _make


class RecordedVision:
    """A vision grader that replays recorded replies by image hash. `calls` lists what it saw."""

    def __init__(self, replies: dict[str, VisionReply] | None = None, *, raises: Exception | None = None) -> None:
        self.replies = replies or {}
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def look(self, *, image: bytes, mime: str, catalogue: str, idempotency_key: str,
             cost_cap_eur: Decimal) -> VisionReply:
        sha = hashlib.sha256(image).hexdigest()
        self.calls.append((sha, catalogue))
        if self.raises is not None:
            raise self.raises
        return self.replies.get(sha, VisionReply(provider="recorded", model="vision-fixture",
                                                 cost_eur=Decimal("0.002"), units=900))


def gate_review(piece: FinishedPiece, rows):
    """What approval decides: the gate's own snapshot, content types and review, nothing filtered first."""
    snap = build_snapshot_for(piece)
    hashes = dict(snap.hash.variants)
    return review_checks(list(rows), tenant_id=T, hashes=hashes,
                         types={vid: content_types(snap.payload(vid)) for vid in hashes})


def build_snapshot_for(piece: FinishedPiece):
    from thallo.contracts.models import Bundle
    v = piece.content.variant
    bundle = Bundle(id=v.bundle_id, created_at=v.created_at, tenant_id=T, asset_id=_id(52), rule_version=1)
    from thallo.gate.canonical import BundleContent
    return build_snapshot(BundleContent(bundle=bundle, variants=(piece.content,)), NO_LINKS, frozenset())
