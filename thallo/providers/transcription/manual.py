"""Manual transcription: a person types the transcript."""
from __future__ import annotations

from thallo.connections.manual_base import ManualProvider, Outcome
from thallo.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode


class ManualTranscription(ManualProvider):
    capability = Capability.TRANSCRIPTION

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.TranscriptionRequest)
        if request.typed_text:
            return Outcome(c.TranscriptionResult(text=request.typed_text, language=request.language_hint),
                           title="Transcript typed by a person", done=True)
        return Outcome(
            c.TranscriptionResult(text="", language=request.language_hint),
            title="Type what is said in the recording",
            steps=("Listen to the recording.", "Type the transcript, in the language spoken."),
            details={"media_sha256": request.media_sha256})


def create(ctx: ProviderContext) -> ManualTranscription:
    return ManualTranscription(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TRANSCRIPTION, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
