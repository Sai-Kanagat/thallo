"""Shared machinery for the HTTP transcription providers (local and hosted).

Both speak the OpenAI-compatible transcription shape, which a hosted vendor and the common local
servers (speaches / faster-whisper-server, whisper.cpp's server with
`--inference-path /v1/audio/transcriptions`) all accept:

    POST {base}/audio/transcriptions        multipart/form-data
         file=<audio bytes>, model=<connection model>, response_format=json|verbose_json,
         language=<en|it|ta, when the request has a hint>
    reply: {"text": "..."}  (verbose_json adds language, duration in seconds, segments)

What this file holds, and why:
- Audio never travels in the request (the contract carries only `media_sha256`). A provider reads
  the bytes back from the tenant's own folder with an `AudioReader` (`audio.py`), re-hashes them
  and refuses on any difference, so the bytes transcribed are the bytes the person recorded.
- `RawTransport` is the text providers' transport rules on a bytes body: the same SSRF policy and
  pinned address (`address_allowed`, `pinned_connection` from `providers/text/base.py`), no
  redirects, the secret revealed only while the header is built and popped in `finally`, errors
  that carry a type name and never a body, an address or the secret.
- Origin binding as in text: the operator fixes where a secret may go (`allowed_origins()`), and
  `_send` refuses any other origin before the transport sees the call.
- Nothing here builds a prompt. The OpenAI `prompt` field is never sent, so no tenant text can
  steer the model; the transcript that comes back is untrusted data for the caller to store.
- Money: priced per audio minute in USD (per connection), converted at the one fixed rate. The
  estimate is an upper bound from the file size at a floor bitrate; the run is charged on the
  duration the vendor reports when it reports one, else on the estimate.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thallo.connections.errors import ConnectionsError, InvalidConnection, ProviderUnavailable
from thallo.connections.registry import ProviderContext
from thallo.connections.secret import Secret
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, Language
from thallo.contracts.testing.mocks import MockProvider
from thallo.providers.text.base import (
    MAX_BODY_BYTES,
    MODEL_ID,
    AddressPolicy,
    Auth,
    ConnectionFactory,
    HttpReply,
    Origin,
    Resolver,
    address_allowed,
    check_header_secret,
    parse_origin,
    pinned_connection,
    status_class,
    system_resolver,
    to_eur,
)

FLOOR_BITS_PER_SECOND = 6_000  # below any speech codec a browser records with, so the estimate is an upper bound
MAX_SEGMENTS = 2_000
LANGUAGES = {"en": Language.EN, "english": Language.EN, "it": Language.IT, "italian": Language.IT,
             "ta": Language.TA, "tamil": Language.TA}


class TranscriptionError(ConnectionsError):
    """The vendor failed or answered in an unexpected shape. Never carries a body or a secret."""


AudioReader = Callable[[str, str], bytes]  # (tenant_id, sha256) -> the recording's bytes


# ====================================================================== transport

@dataclass(frozen=True)
class RawCall:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    auth: Auth | None = None
    timeout: float = 120.0


RawTransportFn = Callable[[RawCall], HttpReply]


class RawTransport:
    """The real transport for bytes bodies; same rules as `providers/text/base.HttpTransport`."""

    def __init__(self, policy: AddressPolicy, *, resolver: Resolver = system_resolver,
                 connect: ConnectionFactory = pinned_connection) -> None:
        self.policy = policy
        self.resolver = resolver
        self.connect = connect

    def __call__(self, call: RawCall) -> HttpReply:
        if call.auth is not None:
            check_header_secret(call.auth.secret)
        parts = urlsplit(call.url)
        allowed = ("https",) if self.policy is AddressPolicy.PUBLIC else ("http", "https")
        if parts.scheme not in allowed or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise TranscriptionError("the endpoint URL is not allowed")
        host = parts.hostname
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise TranscriptionError("the endpoint URL is not allowed") from None
        ips = list(self.resolver(host, port))
        if not ips or not all(address_allowed(ip, self.policy) for ip in ips):
            raise TranscriptionError(f"the endpoint resolves to an address outside the {self.policy.value} policy")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = {"accept": "application/json", **call.headers}
        conn = self.connect(parts.scheme, host, port, ips[0], call.timeout)
        failed: str | None = None
        status, raw = 0, b""
        try:
            if call.auth is not None:
                headers[call.auth.header] = call.auth.scheme + call.auth.secret.reveal()
            conn.request(call.method, path, body=call.body, headers=headers)
            resp = conn.getresponse()
            status, raw = resp.status, resp.read(MAX_BODY_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
            failed = type(exc).__name__
        finally:
            if call.auth is not None:
                headers.pop(call.auth.header, None)
            conn.close()
        if failed is not None:
            raise TranscriptionError(f"could not reach the endpoint ({failed})")
        if 300 <= status < 400:
            raise TranscriptionError("the endpoint answered with a redirect (HTTP 3xx); redirects are refused")
        if len(raw) > MAX_BODY_BYTES:
            raise TranscriptionError("the endpoint's answer is too large")
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        return HttpReply(status, body)


def multipart(fields: Mapping[str, str], file_field: str, filename: str, file_mime: str,
              data: bytes) -> tuple[bytes, str]:
    """(body, content type). The boundary comes from the content's hash (no clock, no randomness)
    and is lengthened until it appears nowhere in the body."""
    seed = hashlib.sha256(data).hexdigest()
    boundary = f"thallo{seed[:24]}"
    while boundary.encode() in data or any(boundary in v for v in fields.values()):
        seed = hashlib.sha256(seed.encode()).hexdigest()
        boundary += seed[:8]
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {file_mime}\r\n\r\n").encode()
    out += data
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def sniff_audio(data: bytes) -> tuple[str, str]:
    """(file name, mime) for the upload, from the first bytes; vendors pick the decoder by name."""
    head = data[:12]
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio.webm", "audio/webm"
    if head.startswith(b"OggS"):
        return "audio.ogg", "audio/ogg"
    if head[4:8] == b"ftyp":
        return "audio.m4a", "audio/mp4"
    if head.startswith(b"RIFF"):
        return "audio.wav", "audio/wav"
    return "audio.mp3", "audio/mpeg"


# ====================================================================== settings

RESPONSE_FORMATS = ("json", "verbose_json")


class TranscriptionSettings(BaseModel):
    """Tenant-writable: no endpoint and no secret here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    usd_per_minute: Decimal = Field(ge=0)
    response_format: str = Field(default="json", pattern=r"^(json|verbose_json)$")
    timeout_s: float = Field(default=300.0, gt=0, le=1800)


@dataclass(frozen=True)
class Parsed:
    text: str
    language: Language | None
    duration_s: float | None
    segments: tuple[dict[str, Any], ...]


# ====================================================================== the base provider

class HttpTranscriptionProvider:
    capability: ClassVar[Capability] = Capability.TRANSCRIPTION
    name: ClassVar[str]
    mode: ClassVar[ConnectionMode] = ConnectionMode.HOSTED
    needs_secret: ClassVar[bool] = True
    sends_externally: ClassVar[bool] = True
    licence_note: ClassVar[str | None] = None
    Settings: ClassVar[type[TranscriptionSettings]] = TranscriptionSettings
    address_policy: ClassVar[AddressPolicy] = AddressPolicy.PUBLIC
    model_id: ClassVar[re.Pattern[str]] = MODEL_ID

    def __init__(self, ctx: ProviderContext, *, transport: RawTransportFn | None = None,
                 audio: AudioReader | None = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.model = ctx.connection.model
        self.transport: RawTransportFn = transport or RawTransport(self.address_policy)
        self.audio = audio
        try:
            self.settings: TranscriptionSettings | None = self.Settings.model_validate(ctx.connection.settings)
        except ValidationError:
            self.settings = None

    # ------------------------------------------------------------------ vendor specifics

    def _base(self) -> str:
        raise NotImplementedError

    def allowed_origins(self) -> frozenset[Origin]:
        raise NotImplementedError

    def auth(self, secret: Secret) -> Auth:
        return Auth("authorization", secret, "Bearer ")

    def health_call(self, auth: Auth | None) -> RawCall:
        return RawCall("GET", f"{self._base()}/models", {}, None, auth, 10.0)

    # ------------------------------------------------------------------ configuration

    def problem(self) -> str | None:
        if not self.model:
            return "no model is set on this connection"
        if not self.model_id.match(self.model):
            return "the model id on this connection is not a valid id"
        if self.settings is None:
            return "the connection settings do not match this provider's schema"
        if self.needs_secret and (self.ctx.connection.secret_ref is None or self.ctx.vault is None):
            return "no secret is stored for this connection"
        if self.audio is None:
            return "the operator has not configured where recordings are read from"
        try:
            allowed = self.allowed_origins()
        except ValueError:
            return "the operator's list of allowed endpoints is not a list of origins"
        if not allowed:
            return "the operator has not allowed any endpoint for this provider"
        try:
            here = parse_origin(self._base(), schemes=self.schemes())
        except ValueError:
            return "the endpoint is not a canonical URL"
        return None if here in allowed else "the endpoint is not one the operator allowed"

    def schemes(self) -> tuple[str, ...]:
        return ("https",) if self.address_policy is AddressPolicy.PUBLIC else ("http", "https")

    def _send(self, call: RawCall) -> HttpReply:
        try:
            ok = parse_origin(call.url, schemes=self.schemes()) in self.allowed_origins()
        except ValueError:
            ok = False
        if not ok:
            raise TranscriptionError("refused a call to an endpoint the operator did not allow")
        return self.transport(call)

    def _auth(self) -> Auth | None:
        if not self.needs_secret:
            return None
        secret = self.ctx.secret()
        if secret is None or not secret:
            raise TranscriptionError("the vault returned no secret for this connection")
        check_header_secret(secret)
        return self.auth(secret)

    def _check_tenant(self, request: c.Request) -> None:
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")

    def _recording(self, request: c.TranscriptionRequest) -> bytes:
        assert self.audio is not None
        try:
            data = self.audio(request.tenant_id, request.media_sha256)
        except (OSError, ValueError, LookupError):
            raise ProviderUnavailable("the recording is not in the tenant's folder") from None
        if hashlib.sha256(data).hexdigest() != request.media_sha256:
            raise ProviderUnavailable("the stored recording does not match its hash; it was not sent")
        return data

    # ------------------------------------------------------------------ the five calls

    def describe(self) -> c.ProviderInfo:
        s = self.settings
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=self.mode,
                              models=(self.model,) if self.model else (),
                              unit_cost_eur={} if s is None else {"per_minute": to_eur(s.usd_per_minute)},
                              limits={"configured": self.problem() is None}, licence_note=self.licence_note,
                              sends_externally=self.sends_externally)

    def health(self) -> c.Health:
        now = self.clock.now()
        if (issue := self.problem()) is not None:
            return c.Health(connected=False, checked_at=now, detail=issue)
        try:
            reply = self._send(self.health_call(self._auth()))
        except ConnectionsError as exc:
            return c.Health(connected=False, checked_at=now, detail=type(exc).__name__)
        ok = 200 <= reply.status < 300
        return c.Health(connected=ok, checked_at=now,
                        detail=None if ok else f"health check answered HTTP {status_class(reply.status)}")

    def _cost(self, seconds: float) -> Decimal:
        assert self.settings is not None
        minutes = Decimal(str(round(seconds, 3))) / Decimal(60)
        return to_eur(minutes * self.settings.usd_per_minute)

    def _upper_seconds(self, size: int) -> float:
        return size * 8 / FLOOR_BITS_PER_SECOND

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        self._check_tenant(request)
        assert isinstance(request, c.TranscriptionRequest)
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"the {self.name} connection is not configured: {issue}")
        seconds = self._upper_seconds(len(self._recording(request)))
        return c.Estimate(cost_eur=self._cost(seconds), seconds=1.0 + seconds / 4)

    def build_call(self, request: c.TranscriptionRequest, data: bytes, auth: Auth | None) -> RawCall:
        assert self.settings is not None and self.model is not None
        fields = {"model": self.model, "response_format": self.settings.response_format}
        if request.language_hint is not None:
            fields["language"] = request.language_hint.value
        filename, mime = sniff_audio(data)
        body, ctype = multipart(fields, "file", filename, mime, data)
        return RawCall("POST", f"{self._base()}/audio/transcriptions", {"content-type": ctype}, body, auth,
                       self.settings.timeout_s)

    def parse(self, reply: HttpReply) -> Parsed:
        b = reply.body
        if not isinstance(b, dict) or not isinstance(b.get("text"), str):
            raise TranscriptionError(f"{self.name} returned an unexpected response shape (text)")
        lang = b.get("language")
        duration = b.get("duration")
        segs = b.get("segments") if isinstance(b.get("segments"), list) else []
        segments = tuple(
            {"start": float(s["start"]), "end": float(s["end"]), "text": s["text"]}
            for s in segs[:MAX_SEGMENTS]
            if isinstance(s, dict) and isinstance(s.get("text"), str)
            and isinstance(s.get("start"), int | float) and isinstance(s.get("end"), int | float))
        return Parsed(text=b["text"].strip(), language=LANGUAGES.get(lang.lower()) if isinstance(lang, str) else None,
                      duration_s=float(duration) if isinstance(duration, int | float) and duration >= 0 else None,
                      segments=segments)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)
        self._check_tenant(request)
        assert isinstance(request, c.TranscriptionRequest)
        c.check_cap(self.estimate(request), request)
        data = self._recording(request)
        now = self.clock.now()
        reply = self._send(self.build_call(request, data, self._auth()))
        if not 200 <= reply.status < 300:
            raise TranscriptionError(f"{self.name} answered HTTP {status_class(reply.status)}")
        parsed = self.parse(reply)
        seconds = parsed.duration_s if parsed.duration_s is not None else self._upper_seconds(len(data))
        return c.RunResult(
            output=c.TranscriptionResult(text=parsed.text, language=parsed.language or request.language_hint,
                                         segments=parsed.segments),
            cost_eur=self._cost(seconds), seconds=0.0,
            provenance=c.Provenance(made_by="provider", provider=self.name, mode=self.mode, model=self.model,
                                    made_at=now, settings={"response_format": self.settings.response_format
                                                           if self.settings else None,
                                                           "duration_s": parsed.duration_s}))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.name}")


__all__ = ["AudioReader", "HttpTranscriptionProvider", "RawCall", "RawTransport", "TranscriptionError",
           "TranscriptionSettings", "multipart", "sniff_audio"]
