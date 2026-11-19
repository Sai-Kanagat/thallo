"""Speech to text on this machine: a loopback server speaking the OpenAI-compatible shape
(speaches / faster-whisper-server, or whisper.cpp's server started with
`--inference-path /v1/audio/transcriptions`).

No secret and no outside party: the endpoint must be loopback, so `sends_externally` is false and
the audio never leaves the machine. Price defaults to zero.

The endpoint is operator configuration, never tenant data. The operator sets, in the environment:
    THALLO_LOCAL_TRANSCRIPTION_URL      the loopback base URL including /v1
                                        (default http://127.0.0.1:8080/v1)
    THALLO_LOCAL_TRANSCRIPTION_TENANTS  comma-separated tenant ids allowed to use it, or "*"
    THALLO_BRAND_ROOT                   where capture stored the recordings
A tenant not on the list gets an unconfigured provider: no call, and the chain moves on to the
next connection or the manual floor (a person types the transcript).
Request and reply shapes follow the servers' docs as of Nov 2026; the test fixtures are written by
hand from them, not captured.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal

from pydantic import Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.text.base import AddressPolicy, Auth, Origin, check_local, parse_origin
from thallo.providers.transcription.audio import operator_audio_reader
from thallo.providers.transcription.base import (
    AudioReader,
    HttpTranscriptionProvider,
    RawCall,
    RawTransportFn,
    TranscriptionError,
    TranscriptionSettings,
)

NAME = "local"
DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
URL_ENV = "THALLO_LOCAL_TRANSCRIPTION_URL"
TENANTS_ENV = "THALLO_LOCAL_TRANSCRIPTION_TENANTS"


class LocalTranscriptionSettings(TranscriptionSettings):
    usd_per_minute: Decimal = Field(default=Decimal(0), ge=0)


def operator_endpoint(tenant_id: str, environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    allowed = {t.strip() for t in environ.get(TENANTS_ENV, "").split(",") if t.strip()}
    if "*" not in allowed and tenant_id not in allowed:
        return None, "the operator has not enabled local transcription for this tenant"
    try:
        return check_local(environ.get(URL_ENV, DEFAULT_BASE_URL)), None
    except ValueError:
        return None, "the operator's local transcription endpoint is not a loopback URL"


class LocalTranscription(HttpTranscriptionProvider):
    name = NAME
    mode = ConnectionMode.LOCAL
    needs_secret = False
    sends_externally = False
    licence_note = "the local model's own licence; nothing leaves this machine"
    Settings = LocalTranscriptionSettings
    address_policy = AddressPolicy.LOOPBACK

    def __init__(self, ctx: ProviderContext, *, transport: RawTransportFn | None = None,
                 audio: AudioReader | None = None, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        super().__init__(ctx, transport=transport, audio=audio if audio is not None else operator_audio_reader(env))
        self.base_url, self._operator_problem = operator_endpoint(ctx.tenant_id, env)

    def problem(self) -> str | None:
        return self._operator_problem or super().problem()

    def allowed_origins(self) -> frozenset[Origin]:
        return frozenset({parse_origin(self.base_url, schemes=self.schemes())}) if self.base_url else frozenset()

    def auth(self, secret: Secret) -> Auth:  # pragma: no cover - needs_secret is False
        raise TranscriptionError("the local endpoint takes no secret")

    def _base(self) -> str:
        assert self.base_url is not None
        return self.base_url

    def health_call(self, auth: Auth | None) -> RawCall:
        return RawCall("GET", f"{self._base()}/models", {}, None, None, 5.0)


def create(ctx: ProviderContext, transport: RawTransportFn | None = None, audio: AudioReader | None = None,
           environ: Mapping[str, str] | None = None) -> LocalTranscription:
    return LocalTranscription(ctx, transport=transport, audio=audio, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TRANSCRIPTION, NAME, ConnectionMode.LOCAL, create,
                      settings_schema=LocalTranscriptionSettings)
