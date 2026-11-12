"""Manual ads: a person makes the change on the platform and follows a checklist. No spend moves from here."""
from __future__ import annotations

from cortex_operator.connections.manual_base import ManualProvider, Outcome
from cortex_operator.connections.registry import NoSettings, ProviderContext, ProviderRegistry
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.enums import Capability, ConnectionMode


class ManualAds(ManualProvider):
    capability = Capability.ADS

    def fulfil(self, request: c.Request) -> Outcome:
        assert isinstance(request, c.AdsRequest)
        steps: tuple[str, ...]
        match request.action:
            case c.AdsAction.CREATE_PAUSED:
                steps = (f"Create the campaign on {request.platform}.", "Leave it paused. Do not enable it.")
            case c.AdsAction.PAUSE:
                steps = (f"Pause campaign {request.campaign_external_id} on {request.platform}.",)
            case c.AdsAction.APPLY_SIGNED:
                steps = (f"Set the daily budget of {request.campaign_external_id} on {request.platform} to "
                         f"{request.currency} {request.to_daily}, exactly the signed amount.",
                         "Change nothing else.", "Report the live value back.")
            case c.AdsAction.PULL_SPEND:
                steps = (f"Export the spend report from {request.platform} as CSV.", "Upload it.")
            case c.AdsAction.UPLOAD_OUTCOMES:
                steps = (f"Upload the outcomes on {request.platform} by hand.",)
        return Outcome(
            c.AdsResult(status="manual_checklist", rows=tuple({"step": i + 1, "do": s} for i, s in enumerate(steps))),
            title="Ads checklist", steps=steps, details={"action": request.action.value})


def create(ctx: ProviderContext) -> ManualAds:
    return ManualAds(ctx.clock)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.ADS, "manual", ConnectionMode.MANUAL, create, settings_schema=NoSettings)
