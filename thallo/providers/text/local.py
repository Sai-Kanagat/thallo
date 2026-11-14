"""A model running on this machine, through Ollama's native chat API.

    POST {base_url}/api/chat
    body:   model, messages=[{role: system, content}, {role: user, content}], stream: false,
            options={num_predict}
    reply:  model, created_at, message={role, content}, done, done_reason, prompt_eval_count,
            eval_count

No secret and no outside party: the endpoint must be loopback (localhost, 127.0.0.1 or ::1), so
`sends_externally` is false. Prices default to zero; set them if the machine's cost should count.
Shape from Ollama's API docs as of Nov 2026; the test fixtures are written by hand from it, not
captured.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.text.base import (
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


class LocalSettings(PricedSettings):
    base_url: str = DEFAULT_BASE_URL
    input_usd_per_mtok: Decimal = Field(default=Decimal(0), ge=0)
    output_usd_per_mtok: Decimal = Field(default=Decimal(0), ge=0)
    timeout_s: float = Field(default=300.0, gt=0, le=1800)

    @field_validator("base_url")
    @classmethod
    def _loopback(cls, v: str) -> str:
        return check_local(v)


class LocalText(HttpTextProvider):
    name = NAME
    mode = ConnectionMode.LOCAL
    needs_secret = False
    sends_externally = False
    licence_note = "the local model's own licence; nothing leaves this machine"
    Settings = LocalSettings

    def auth(self, secret: Secret) -> Auth:  # pragma: no cover - needs_secret is False
        raise TextProviderError("the local endpoint takes no secret")

    def _base(self) -> str:
        assert isinstance(self.settings, LocalSettings)
        return self.settings.base_url

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
        return Parsed(content.strip(), tokens(b.get("prompt_eval_count")), tokens(b.get("eval_count")),
                      None, b.get("model") if isinstance(b.get("model"), str) else None)

    def health_call(self, auth: Auth | None) -> HttpCall:
        return HttpCall("GET", f"{self._base()}/api/tags", {}, None, None, 5.0)

    def health_ok(self, reply: HttpReply) -> bool:
        models = reply.body.get("models") if isinstance(reply.body, dict) else None
        return isinstance(models, list) and any(isinstance(m, dict) and m.get("name") == self.model for m in models)


def create(ctx: ProviderContext, transport: Transport | None = None) -> LocalText:
    return LocalText(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TEXT, NAME, ConnectionMode.LOCAL, create, settings_schema=LocalSettings)
