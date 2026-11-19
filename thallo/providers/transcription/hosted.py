"""Hosted speech to text over the OpenAI-compatible transcription API (OpenAI, Groq and others
that copy its shape). The audio goes to a third party: `sends_externally` is true, and the
connections screen says so before anyone turns it on.

    POST {base_url}/audio/transcriptions   multipart: file, model, response_format, language
    GET  {base_url}/models                 health
    Authorization: Bearer <secret from the vault>

Where the secret may go is operator configuration, never tenant data:
    THALLO_TRANSCRIPTION_ORIGINS   comma-separated https origins the operator allows
                                   (for example https://api.openai.com)
    THALLO_BRAND_ROOT              where capture stored the recordings
The connection's `base_url` setting picks one of those origins plus a path (".../v1"); a base_url
on any other origin leaves the provider unconfigured, so no call and no secret ever leave.
Request and reply shapes follow the vendors' docs as of Nov 2026; the test fixtures are written
by hand from them, not captured.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import field_validator

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.text.base import Origin, check_https, parse_origin_list
from thallo.providers.transcription.audio import operator_audio_reader
from thallo.providers.transcription.base import (
    AudioReader,
    HttpTranscriptionProvider,
    RawTransportFn,
    TranscriptionSettings,
)

NAME = "hosted"
ORIGINS_ENV = "THALLO_TRANSCRIPTION_ORIGINS"


class HostedTranscriptionSettings(TranscriptionSettings):
    base_url: str

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        return check_https(v)


class HostedTranscription(HttpTranscriptionProvider):
    name = NAME
    mode = ConnectionMode.HOSTED
    needs_secret = True
    sends_externally = True
    licence_note = "the vendor's terms for audio sent to its API"
    Settings = HostedTranscriptionSettings

    def __init__(self, ctx: ProviderContext, *, transport: RawTransportFn | None = None,
                 audio: AudioReader | None = None, environ: Mapping[str, str] | None = None) -> None:
        self.environ = os.environ if environ is None else environ
        super().__init__(ctx, transport=transport,
                         audio=audio if audio is not None else operator_audio_reader(self.environ))

    def allowed_origins(self) -> frozenset[Origin]:
        return parse_origin_list(self.environ.get(ORIGINS_ENV, ""))

    def _base(self) -> str:
        assert isinstance(self.settings, HostedTranscriptionSettings)
        return self.settings.base_url


def create(ctx: ProviderContext, transport: RawTransportFn | None = None, audio: AudioReader | None = None,
           environ: Mapping[str, str] | None = None) -> HostedTranscription:
    return HostedTranscription(ctx, transport=transport, audio=audio, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TRANSCRIPTION, NAME, ConnectionMode.HOSTED, create,
                      settings_schema=HostedTranscriptionSettings)
