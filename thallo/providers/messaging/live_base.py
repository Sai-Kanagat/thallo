"""Shared code for the messaging providers that can reach a person (WhatsApp Cloud, Resend, SMTP).
No `register` here.

Three locks, all of which must be open before anything leaves this machine:

1. The operator switch. `THALLO_MESSAGING_LIVE_SEND=1` in the operator's environment (never a
   connection setting). Off by default: `estimate()` and `run()` raise `ProviderUnavailable`, so the
   registry falls through to the manual connection, which returns a click-to-chat or mailto link
   that opens the seller's own app. That is the draft-only mode.
2. A person. The request must carry `human` (the lead desk adds it after `require_human`). A
   template-only request is refused: no message goes out on a template approval alone.
3. A configured connection: settings valid for the provider and a secret in the vault.

Hosts are constants in code or come from the operator's environment, never from settings.
Failures say whether the message may have left (`MessageOutcomeUnknown`) or surely did not
(`MessageNotSent`); the lead desk records either and never resends by itself.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from thallo.connections.errors import ConnectionsError, InvalidConnection, ProviderUnavailable
from thallo.connections.registry import ProviderContext
from thallo.connections.secret import Secret
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, MessageChannel
from thallo.contracts.testing.mocks import MockProvider

LIVE_ENV = "THALLO_MESSAGING_LIVE_SEND"


class MessageNotSent(ConnectionsError):
    """Nothing reached the recipient's provider."""


class MessageOutcomeUnknown(ConnectionsError):
    """The request may have been received; a person checks before sending again."""


def live_switch_on(environ: Mapping[str, str] | None) -> bool:
    import os

    return (os.environ if environ is None else environ).get(LIVE_ENV) == "1"


class LiveMessagingProvider:
    capability: ClassVar[Capability] = Capability.MESSAGING
    name: ClassVar[str]
    mode: ClassVar[ConnectionMode] = ConnectionMode.HOSTED
    channels: ClassVar[frozenset[MessageChannel]]
    settings_model: ClassVar[type[BaseModel]]
    licence_note: ClassVar[str]

    def __init__(self, ctx: ProviderContext, *, transport: Any = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.environ = environ
        self.transport = transport
        try:
            self.settings: Any = self.settings_model.model_validate(ctx.connection.settings)
        except ValidationError:
            self.settings = None

    # ------------------------------------------------------------------ configuration
    def extra_problem(self) -> str | None:
        return None

    def problem(self) -> str | None:
        if self.settings is None:
            return f"the connection settings do not match the {self.name} schema"
        if self.ctx.connection.secret_ref is None or self.ctx.vault is None:
            return "no credential is stored for this connection"
        return self.extra_problem()

    def secret(self) -> Secret:
        s = self.ctx.secret()
        if s is None or not s:
            raise MessageNotSent("the vault returned no credential for this connection")
        return s

    # ------------------------------------------------------------------ the five calls
    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=self.mode,
                              limits={"configured": self.problem() is None, "live_send": live_switch_on(self.environ),
                                      "channels": sorted(ch.value for ch in self.channels),
                                      "needs_human_per_message": True},
                              licence_note=self.licence_note, sends_externally=True)

    def health(self) -> c.Health:
        now = self.clock.now()
        if (issue := self.problem()) is not None:
            return c.Health(connected=False, checked_at=now, detail=issue)
        if not live_switch_on(self.environ):
            return c.Health(connected=False, checked_at=now,
                            detail="live sending is off: messages open in the seller's own app")
        return c.Health(connected=True, checked_at=now)

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"the {self.name} connection is not configured: {issue}")
        if not live_switch_on(self.environ):
            raise ProviderUnavailable("live sending is off; the manual connection opens the seller's own app")
        assert isinstance(request, c.MessageSendRequest)
        if request.human is None:
            raise ProviderUnavailable("only a person's tap sends a message; a template approval alone never does")
        if request.channel not in self.channels:
            raise ProviderUnavailable(f"{self.name} does not carry {request.channel.value}")
        return c.Estimate(cost_eur=self.unit_cost(request), seconds=2.0)

    def unit_cost(self, request: c.MessageSendRequest) -> Decimal:
        return Decimal(0)

    def run(self, request: c.Request) -> c.RunResult:
        est = self.estimate(request)
        c.check_cap(est, request)
        assert isinstance(request, c.MessageSendRequest)
        external_id = self.send(request)
        return c.RunResult(output=c.MessageSendResult(status="sent", external_id=external_id), cost_eur=est.cost_eur,
                           provenance=c.Provenance(made_by="provider", provider=self.name, mode=self.mode,
                                                   made_at=self.clock.now()),
                           external_ids={"message_id": external_id})

    def send(self, request: c.MessageSendRequest) -> str:
        raise NotImplementedError

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.name}")


__all__ = ["LIVE_ENV", "LiveMessagingProvider", "MessageNotSent", "MessageOutcomeUnknown", "live_switch_on"]


# ====================================================================== HTTPS JSON sends

def post_json(provider: LiveMessagingProvider, url: str, payload: dict[str, Any], auth: Any,
              timeout: float = 20.0) -> Any:
    """One POST through the guarded transport (https only, public address pinned, no redirects).
    Returns the parsed body of a 2xx answer, else raises NotSent or OutcomeUnknown."""
    import json

    from thallo.providers.publishing.postiz_base import PostizTransport, RawCall, TransportError
    from thallo.providers.text.base import status_class

    transport = provider.transport or PostizTransport()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        reply = transport(RawCall("POST", url, body, "application/json", auth, timeout))
    except TransportError as exc:
        if exc.reached:
            raise MessageOutcomeUnknown(f"{provider.name}: {exc}") from None
        raise MessageNotSent(f"{provider.name}: {exc}") from None
    if 400 <= reply.status < 500:
        raise MessageNotSent(f"{provider.name} refused the message (HTTP {status_class(reply.status)})")
    if not 200 <= reply.status < 300:
        raise MessageOutcomeUnknown(f"{provider.name} answered HTTP {status_class(reply.status)}; it may have sent")
    return reply.body
