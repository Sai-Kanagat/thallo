"""Claude, through the Messages API over plain HTTPS (no SDK is a dependency).

    POST https://api.anthropic.com/v1/messages
    headers: x-api-key, anthropic-version
    body:    model, max_tokens, system, messages=[{role: user, content: [{type: text, text}]}]
    reply:   id, model, content=[{type: text, text} | {type: thinking, ...}], stop_reason,
             usage={input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}

Shape and version from Anthropic's API reference as of Nov 2026; the test fixtures are written by
hand from it, not captured. The model is the connection row's (for example claude-sonnet-5-5 for
DRAFT and claude-haiku-5-5 for CLASSIFY); prices are the connection's settings.

The API key goes only to `BASE_URL`, a constant: there is no base_url setting, and a connection
row that still carries one does not parse, so the provider reports itself unconfigured.
"""
from __future__ import annotations

from typing import Literal

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.text.base import (
    Auth,
    HttpCall,
    HttpReply,
    HttpTextProvider,
    Origin,
    Parsed,
    PricedSettings,
    TextProviderError,
    Transport,
    need,
    parse_origin,
    system_text,
    tokens,
    user_text,
)

NAME = "claude"
BASE_URL = "https://api.anthropic.com"
ORIGINS: frozenset[Origin] = frozenset({parse_origin(BASE_URL)})
API_VERSION = "2026-10-21"


class ClaudeSettings(PricedSettings):
    """Prices, timeout and effort: no endpoint, because settings are tenant-writable."""

    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class ClaudeText(HttpTextProvider):
    name = NAME
    mode = ConnectionMode.HOSTED
    licence_note = "Anthropic commercial terms; prompts and outputs leave this system"
    Settings = ClaudeSettings

    def auth(self, secret: Secret) -> Auth:
        return Auth("x-api-key", secret)

    def allowed_origins(self) -> frozenset[Origin]:
        return ORIGINS

    def _base(self) -> str:
        return BASE_URL

    def build_call(self, request: c.TextRequest, auth: Auth | None) -> HttpCall:
        assert isinstance(self.settings, ClaudeSettings)
        body: dict = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": system_text(request),
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_text(request)}]}],
        }
        if self.settings.effort is not None:
            body["output_config"] = {"effort": self.settings.effort}
        return HttpCall("POST", f"{self._base()}/v1/messages", {"anthropic-version": API_VERSION}, body, auth,
                        self.settings.timeout_s)

    def parse(self, reply: HttpReply) -> Parsed:
        b = reply.body
        need(isinstance(b, dict) and isinstance(b.get("content"), list), NAME, "content")
        stop = b.get("stop_reason")
        if stop == "refusal":
            raise TextProviderError("claude declined the request (stop_reason refusal)")
        if stop == "max_tokens":
            raise TextProviderError("claude stopped at max_tokens; the output is cut")
        texts = [blk.get("text", "") for blk in b["content"] if isinstance(blk, dict) and blk.get("type") == "text"]
        need(bool(texts), NAME, "no text block")
        tin, tout = self.usage(reply) or (0, 0)
        return Parsed("".join(texts).strip(), tin, tout,
                      b.get("id") if isinstance(b.get("id"), str) else None,
                      b.get("model") if isinstance(b.get("model"), str) else None)

    def usage(self, reply: HttpReply) -> tuple[int, int] | None:
        usage = reply.body.get("usage") if isinstance(reply.body, dict) else None
        if not isinstance(usage, dict):
            return None
        tin = (tokens(usage.get("input_tokens")) + tokens(usage.get("cache_creation_input_tokens"))
               + tokens(usage.get("cache_read_input_tokens")))
        return tin, tokens(usage.get("output_tokens"))

    def health_call(self, auth: Auth | None) -> HttpCall:
        return HttpCall("GET", f"{self._base()}/v1/models/{self.model}", {"anthropic-version": API_VERSION},
                        None, auth, 15.0)

    def health_ok(self, reply: HttpReply) -> bool:
        return isinstance(reply.body, dict) and reply.body.get("id") == self.model


def create(ctx: ProviderContext, transport: Transport | None = None) -> ClaudeText:
    return ClaudeText(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TEXT, NAME, ConnectionMode.HOSTED, create, settings_schema=ClaudeSettings)
