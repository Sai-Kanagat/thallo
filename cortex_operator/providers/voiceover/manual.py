"""Manual voiceover: a person records their own voice."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome, pending_media
from cortex_operator.connections.registry import ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode, MediaKind, MediaProvenance


class ManualVoiceover(ManualProvider):
    capability = Capability.VOICEOVER

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.VoiceoverRequest)
        return Outcome(
            pending_media(request, MediaKind.AUDIO, "audio/mpeg", MediaProvenance.PERSON_RECORDING),
            title="Record the voiceover yourself",
            steps=("Read the text aloud and upload the recording.",),
            details={"text": request.text, "language": request.language.value})


def create(ctx: ProviderContext) -> ManualVoiceover:
    return ManualVoiceover(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.VOICEOVER, "manual", ConnectionMode.MANUAL, create)
