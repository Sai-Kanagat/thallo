"""The bytes the publisher verified, handed to the provider in process.

`PublishRequest` carries the media hashes but no bytes, and the connection contract is frozen. So
the publisher reads each file once through the gate's repository (tenant-confined blob store),
hashes it against `decision.payload`, and puts the verified bytes in a `SendContext` that lives in
a context variable for the length of one provider call. Nothing is written to disk and nothing is
read again: the provider takes the bytes from here, hashes the exact object it puts on the wire,
and refuses on any difference (`SendContext.verify`, `SendContext.media_for`).

A provider that needs the bytes (Postiz) refuses to send when no context is active or when the
context does not match the request, so a caller that skips the publisher cannot reach it with
media. This is defence in depth, not a security boundary: code in the same process can set a
context too. The boundary is the publisher being the only caller of `check_publish`.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

from thallo.contracts import connection as c
from thallo.publish.errors import NotSent


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class StagedMedia:
    position: int
    sha256: str
    mime: str
    alt_text: str | None
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class SendContext:
    """One send, or one reconcile lookup, as the publisher verified it."""

    tenant_id: str
    idempotency_key: str
    channel: str
    text: str
    text_sha256: str
    requested_at: datetime  # the date the provider is given, and the centre of any lookup window
    media: tuple[StagedMedia, ...] = ()

    def verify(self, request: c.Request) -> None:
        """Raise `NotSent` unless the request is the one this context was made for."""
        if request.tenant_id != self.tenant_id or request.idempotency_key != self.idempotency_key:
            raise NotSent("the request is not the one the publisher staged", retryable=False)
        if isinstance(request, c.PublishRequest):
            if request.channel.value != self.channel:
                raise NotSent("the request channel is not the staged one", retryable=False)
            if request.text != self.text or sha256_hex(request.text.encode("utf-8")) != self.text_sha256:
                raise NotSent("the request text is not the approved text", retryable=False)
            if tuple(request.media_sha256) != tuple(m.sha256 for m in self.media):
                raise NotSent("the request media are not the staged media", retryable=False)

    def media_for(self, index: int, expected_sha256: str) -> bytes:
        """The staged bytes for one upload, hashed again here: the caller uploads exactly this object."""
        item = self.media[index]
        data = item.data
        if sha256_hex(data) != expected_sha256 or item.sha256 != expected_sha256:
            raise NotSent(f"media {index}: the bytes to upload do not hash to the approved value", retryable=False)
        return data


_current: ContextVar[SendContext | None] = ContextVar("thallo_send_context", default=None)


@contextmanager
def staged(ctx: SendContext) -> Iterator[SendContext]:
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def current() -> SendContext | None:
    return _current.get()


__all__ = ["SendContext", "StagedMedia", "current", "sha256_hex", "staged"]
