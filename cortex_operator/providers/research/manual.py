"""Manual research: a person pastes their own notes. Results are always untrusted."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome
from cortex_operator.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode


class ManualResearch(ManualProvider):
    capability = Capability.RESEARCH

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.ResearchRequest)
        return Outcome(
            c.ResearchResult(items=()),
            title="Paste your research notes",
            steps=("Paste the notes with the source link and date for each.",),
            details={"query": request.query})


def create(ctx: ProviderContext) -> ManualResearch:
    return ManualResearch(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.RESEARCH, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
