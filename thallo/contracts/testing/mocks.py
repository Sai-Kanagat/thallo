"""A deterministic mock provider for every capability.

Same request in, same result out, no network, no keys, no cost. Used by every test and by
the demo client. WP-02 builds the registry's own manual and mock providers on top of this.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, ConnectionMode, MediaKind, MediaProvenance


def _digest(request: c.Request) -> str:
    blob = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _media(kind: MediaKind, d: str, provenance: MediaProvenance, w: int | None, h: int | None,  # noqa: PLR0917
           duration_ms: int | None = None, disclosure: bool = False) -> c.MediaResult:
    mime = {MediaKind.IMAGE: "image/png", MediaKind.VIDEO: "video/mp4", MediaKind.AUDIO: "audio/mpeg",
            MediaKind.PDF: "application/pdf"}[kind]
    return c.MediaResult(kind=kind, sha256=d, mime=mime, storage_uri=f"mock://media/{d}",
                         provenance=provenance, width=w, height=h, duration_ms=duration_ms,
                         disclosure_required=disclosure)


def _output(request: c.Request, d: str) -> c.Result:  # noqa: PLR0911, PLR0912, C901
    match request:
        case c.TextRequest():
            if request.output_schema is not None:
                return c.TextResult(text="", data={"mock": d[:12]}, input_tokens=100, output_tokens=20)
            return c.TextResult(text=f"[mock {request.role.value}] {d[:12]}", input_tokens=100, output_tokens=20)
        case c.ImageRequest():
            prov = (MediaProvenance.TEMPLATE_RENDER if request.purpose == "template_render"
                    else MediaProvenance.CLIENT_GRAPHIC if request.purpose == "upload"
                    else MediaProvenance.GENERATED_BACKGROUND)
            return _media(MediaKind.IMAGE, d, prov, request.width, request.height,
                          disclosure=prov is MediaProvenance.GENERATED_BACKGROUND)
        case c.VideoRequest():
            prov = (MediaProvenance.GENERATED if request.action == "generate"
                    else MediaProvenance.CLIENT_CLIP if request.action in ("cut", "caption", "upload")
                    else MediaProvenance.TEMPLATE_RENDER)
            return _media(MediaKind.VIDEO, d, prov, request.width, request.height,
                          request.duration_ms or 15000, disclosure=prov is MediaProvenance.GENERATED)
        case c.TranscriptionRequest():
            return c.TranscriptionResult(text=request.typed_text or f"mock transcript {d[:8]}",
                                         language=request.language_hint)
        case c.VoiceoverRequest():
            return _media(MediaKind.AUDIO, d, MediaProvenance.GENERATED, None, None, 10000, disclosure=True)
        case c.PublishRequest():
            return c.PublishResult(status="published", external_id=f"mock-post-{d[:10]}",
                                   live_url=f"https://mock.invalid/p/{d[:10]}")
        case c.ReadbackRequest():
            return c.ReadbackResult(live_text=None, media_changed=None)
        case c.CalendarRequest():
            return c.CalendarResult(mirrored=len(request.slots))
        case c.CrmSyncRequest():
            return c.CrmSyncResult(synced=len(request.records))
        case c.BookingRequest():
            return c.BookingResult(link=f"https://mock.invalid/book/{d[:10]}")
        case c.MessageSendRequest():
            return c.MessageSendResult(status="sent", external_id=f"mock-msg-{d[:10]}")
        case c.AnalyticsRequest():
            return c.AnalyticsResult(rows=tuple({"metric": m, "value": int(d[i:i + 2], 16)}
                                                for i, m in enumerate(request.metrics)))
        case c.AdsRequest():
            return c.AdsResult(status=f"mock-{request.action.value}")
        case c.ResearchRequest():
            return c.ResearchResult(items=({"url": "https://mock.invalid/r", "text": f"mock {d[:8]}"},))
        case c.StorageRequest():
            return c.StorageResult(uri=f"mock://store/{request.sha256}", sha256=request.sha256)
        case c.NotifyRequest():
            return c.NotifyResult(delivered=True)
        case c.HookRequest():
            return c.HookResult(delivered=True)
    raise c.CapabilityMismatch(type(request).__name__)


class MockProvider:
    """Implements the five calls for any capability. Costs are zero; estimates are fixed."""

    def __init__(self, capability: Capability, clock: Clock, name: str | None = None) -> None:
        self.capability = capability
        self.clock = clock
        self.name = name or f"mock-{capability.value}"
        self.calls: list[c.Request] = []

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=ConnectionMode.MOCK,
                              models=("mock-1",), licence_note="test double, no outside calls")

    def health(self) -> c.Health:
        return c.Health(connected=True, checked_at=self.clock.now(), quota_left=Decimal(1_000_000))

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        return c.Estimate(cost_eur=Decimal(0), seconds=0.0)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)
        c.check_cap(self.estimate(request), request)
        self.calls.append(request)
        d = _digest(request)
        out = _output(request, d)
        ext: dict[str, Any] = {}
        if hasattr(out, "external_id") and out.external_id:
            ext["external_id"] = out.external_id
        return c.RunResult(
            output=out, cost_eur=Decimal(0), external_ids=ext,
            provenance=c.Provenance(made_by="provider", provider=self.name, mode=ConnectionMode.MOCK,
                                    model="mock-1", made_at=self.clock.now()),
        )

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, self.name)


def mock_providers(clock: Clock) -> dict[Capability, MockProvider]:
    return {cap: MockProvider(cap, clock) for cap in Capability}
