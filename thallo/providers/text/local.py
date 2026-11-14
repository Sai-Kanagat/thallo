"""A model running on this machine, through Ollama's native chat API.

    POST {base_url}/api/chat
    body:   model, messages=[{role: system, content}, {role: user, content}], stream: false,
            options={num_predict}
    reply:  model, created_at, message={role, content}, done, done_reason, prompt_eval_count,
            eval_count

No secret and no outside party: the endpoint must be loopback (localhost, 127.0.0.1 or ::1), so
`sends_externally` is false. Prices default to zero; set them if the machine's cost should count.

The endpoint is operator configuration, never tenant data: connection settings are written by
tenant-side code, so they hold no URL here. The operator sets, in the environment:
    THALLO_LOCAL_TEXT_URL      the loopback base URL (default http://127.0.0.1:11434)
    THALLO_LOCAL_TEXT_TENANTS  comma-separated tenant ids allowed to use it, or "*"
A tenant not on the list gets an unconfigured provider: no call, a pending task saying why.
Shape from Ollama's API docs as of Nov 2026; the test fixtures are written by hand from it, not
captured.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal

from pydantic import Field

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.text.base import (
    AddressPolicy,
    Auth,
    HttpCall,
    HttpReply,
    HttpTextProvider,
    Parsed,
    PricedSettings,
    TextProviderError,
    Transport,
    check_local,
    need,
    system_text,
    tokens,
    user_text,
)

NAME = "local"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
URL_ENV = "THALLO_LOCAL_TEXT_URL"
TENANTS_ENV = "THALLO_LOCAL_TEXT_TENANTS"


class LocalSettings(PricedSettings):
    """Tenant-writable, so no endpoint here."""

    input_usd_per_mtok: Decimal = Field(default=Decimal(0), ge=0)
    output_usd_per_mtok: Decimal = Field(default=Decimal(0), ge=0)
    timeout_s: float = Field(default=300.0, gt=0, le=1800)


def operator_endpoint(tenant_id: str, environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    """(base_url, problem) from the operator's environment."""
    allowed = {t.strip() for t in environ.get(TENANTS_ENV, "").split(",") if t.strip()}
    if "*" not in allowed and tenant_id not in allowed:
        return None, "the operator has not enabled the local endpoint for this tenant"
    try:
        return check_local(environ.get(URL_ENV, DEFAULT_BASE_URL)), None
    except ValueError:
        return None, "the operator's local endpoint is not a loopback URL"


class LocalText(HttpTextProvider):
    name = NAME
    mode = ConnectionMode.LOCAL
    needs_secret = False
    sends_externally = False
    licence_note = "the local model's own licence; nothing leaves this machine"
    Settings = LocalSettings
    address_policy = AddressPolicy.LOOPBACK

    def __init__(self, ctx: ProviderContext, *, transport: Transport | None = None,
                 environ: Mapping[str, str] | None = None) -> None:
        super().__init__(ctx, transport=transport)
        self.base_url, self._operator_problem = operator_endpoint(ctx.tenant_id,
                                                                  os.environ if environ is None else environ)

    def problem(self) -> str | None:
        return self._operator_problem or super().problem()

    def auth(self, secret: Secret) -> Auth:  # pragma: no cover - needs_secret is False
        raise TextProviderError("the local endpoint takes no secret")

    def _base(self) -> str:
        assert self.base_url is not None
        return self.base_url

    def build_call(self, request: c.TextRequest, auth: Auth | None) -> HttpCall:
        assert self.settings is not None
        body: dict = {"model": self.model,
                      "messages": [{"role": "system", "content": system_text(request)},
                                   {"role": "user", "content": user_text(request)}],
                      "stream": False,
                      "options": {"num_predict": request.max_output_tokens}}
        if request.output_schema is not None:
            body["format"] = "json"
        return HttpCall("POST", f"{self._base()}/api/chat", {}, body, None, self.settings.timeout_s)

    def parse(self, reply: HttpReply) -> Parsed:
        b = reply.body
        need(isinstance(b, dict) and isinstance(b.get("message"), dict), NAME, "message")
        if b.get("done") is not True:
            raise TextProviderError("the local model did not finish")
        if b.get("done_reason") == "length":
            raise TextProviderError("the local model stopped at num_predict; the output is cut")
        content = b["message"].get("content")
        need(isinstance(content, str), NAME, "content")
        tin, tout = self.usage(reply) or (0, 0)
        return Parsed(content.strip(), tin, tout, None, b.get("model") if isinstance(b.get("model"), str) else None)

    def usage(self, reply: HttpReply) -> tuple[int, int] | None:
        b = reply.body
        if not isinstance(b, dict) or ("prompt_eval_count" not in b and "eval_count" not in b):
            return None
        return tokens(b.get("prompt_eval_count")), tokens(b.get("eval_count"))

    def health_call(self, auth: Auth | None) -> HttpCall:
        return HttpCall("GET", f"{self._base()}/api/tags", {}, None, None, 5.0)

    def health_ok(self, reply: HttpReply) -> bool:
        models = reply.body.get("models") if isinstance(reply.body, dict) else None
        return isinstance(models, list) and any(isinstance(m, dict) and m.get("name") == self.model for m in models)


def create(ctx: ProviderContext, transport: Transport | None = None,
           environ: Mapping[str, str] | None = None) -> LocalText:
    return LocalText(ctx, transport=transport, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TEXT, NAME, ConnectionMode.LOCAL, create, settings_schema=LocalSettings)
