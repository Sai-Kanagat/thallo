"""Base for manual providers: a person does the step.

A manual run never makes an outside call. It returns the result type the capability expects,
shaped as a pending task or copy pack, with provenance `made_by="person"` and
`RunResult.status="pending_person"` plus a `task` sentence. When the person has already supplied
the answer in the request (a typed transcript) the status is "done" and there is no task.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar

from thallo.contracts import connection as c
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, ConnectionMode, MediaKind, MediaProvenance
from thallo.contracts.testing.mocks import MockProvider


def request_digest(request: c.Request) -> str:
    blob = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class Outcome:
    output: c.Result
    title: str
    steps: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)
    done: bool = False  # true when the person already supplied the answer in the request


def pending_media(request: c.Request, kind: MediaKind, mime: str, provenance: MediaProvenance,
                  width: int | None = None, height: int | None = None,
                  duration_ms: int | None = None) -> c.MediaResult:
    """A placeholder for a file the person has yet to upload. The hash is of the request, not a file."""
    d = request_digest(request)
    return c.MediaResult(kind=kind, sha256=d, mime=mime, storage_uri=f"manual://pending/{d}",
                         provenance=provenance, width=width, height=height, duration_ms=duration_ms,
                         disclosure_required=False)


class ManualProvider:
    capability: ClassVar[Capability]
    name = "manual"

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def fulfil(self, request: c.Request) -> Outcome:
        raise NotImplementedError

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=ConnectionMode.MANUAL,
                              limits={"needs_person": True}, licence_note="a person does this step",
                              sends_externally=False)

    def health(self) -> c.Health:
        return c.Health(connected=True, checked_at=self.clock.now(), detail="manual mode is always available")

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)  # type: ignore[arg-type]
        return c.Estimate(cost_eur=Decimal(0), seconds=0.0)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)  # type: ignore[arg-type]
        c.check_cap(self.estimate(request), request)
        out = self.fulfil(request)
        task = None if out.done else " ".join([out.title.rstrip(".") + ".", *out.steps])
        settings = {"title": out.title, "steps": list(out.steps), "details": out.details,
                    "ref": request_digest(request)[:16]}
        return c.RunResult(
            output=out.output, cost_eur=Decimal(0),
            status="done" if out.done else "pending_person", task=task,
            provenance=c.Provenance(made_by="person", provider=self.name, mode=ConnectionMode.MANUAL,
                                    settings=settings, made_at=self.clock.now()),
        )

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.capability.value}")
