"""Any endpoint that speaks the OpenAI Chat Completions shape (OpenAI itself, Mistral, Groq,
OpenRouter, Together and similar), over plain HTTPS (no SDK is a dependency).

    POST {base_url}/chat/completions
    header: Authorization: Bearer <key>
    body:   model, messages=[{role: system, content}, {role: user, content}], max_tokens
    reply:  id, model, choices=[{message: {role, content}, finish_reason}],
            usage={prompt_tokens, completion_tokens}

`base_url` is required (it includes the version path, e.g. https://api.openai.com/v1) and must be
https. Some endpoints want `max_completion_tokens` instead of `max_tokens`; `output_limit_field`
says which (a name the registry's secret-key filter accepts). Shape from the OpenAI API reference
as of Nov 2026; the test fixtures are written by hand from it, not captured, and compatible
vendors differ in small ways nobody here has checked.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import field_validator

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
    check_https,
    need,
    system_text,
    tokens,
    user_text,
)

NAME = "openai_compatible"


class OpenAICompatibleSettings(PricedSettings):
    base_url: str
    output_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        return check_https(v)


def chat_body(model: str | None, request: c.TextRequest, max_field: str) -> dict:
    return {"model": model,
            "messages": [{"role": "system", "content": system_text(request)},
                         {"role": "user", "content": user_text(request)}],
            max_field: request.max_output_tokens}


def parse_chat(reply: HttpReply, name: str) -> Parsed:
    b = reply.body
    need(isinstance(b, dict) and isinstance(b.get("choices"), list) and bool(b["choices"]), name, "choices")
    first = b["choices"][0]
    need(isinstance(first, dict), name, "choice")
    reason = first.get("finish_reason")
    if reason == "length":
        raise TextProviderError(f"{name} stopped at the token limit; the output is cut")
    if reason == "content_filter":
        raise TextProviderError(f"{name} filtered the output")
    message = first.get("message")
    need(isinstance(message, dict), name, "message")
    if message.get("refusal"):
        raise TextProviderError(f"{name} declined the request")
    content = message.get("content")
    need(isinstance(content, str), name, "content")
    tin, tout = chat_usage(reply) or (0, 0)
    return Parsed(content.strip(), tin, tout,
                  b.get("id") if isinstance(b.get("id"), str) else None,
                  b.get("model") if isinstance(b.get("model"), str) else None)


def chat_usage(reply: HttpReply) -> tuple[int, int] | None:
    usage = reply.body.get("usage") if isinstance(reply.body, dict) else None
    if not isinstance(usage, dict):
        return None
    return tokens(usage.get("prompt_tokens")), tokens(usage.get("completion_tokens"))


class OpenAICompatibleText(HttpTextProvider):
    name = NAME
    mode = ConnectionMode.HOSTED
    licence_note = "the endpoint vendor's terms; prompts and outputs leave this system"
    Settings = OpenAICompatibleSettings
    model_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")  # vendor/model ids; sent in the body only

    def auth(self, secret: Secret) -> Auth:
        return Auth("authorization", secret, "Bearer ")

    def _base(self) -> str:
        assert isinstance(self.settings, OpenAICompatibleSettings)
        return self.settings.base_url

    def build_call(self, request: c.TextRequest, auth: Auth | None) -> HttpCall:
        assert isinstance(self.settings, OpenAICompatibleSettings)
        return HttpCall("POST", f"{self._base()}/chat/completions", {},
                        chat_body(self.model, request, self.settings.output_limit_field), auth, self.settings.timeout_s)

    def parse(self, reply: HttpReply) -> Parsed:
        return parse_chat(reply, NAME)

    def usage(self, reply: HttpReply) -> tuple[int, int] | None:
        return chat_usage(reply)

    def health_call(self, auth: Auth | None) -> HttpCall:
        return HttpCall("GET", f"{self._base()}/models", {}, None, auth, 15.0)

    def health_ok(self, reply: HttpReply) -> bool:
        data = reply.body.get("data") if isinstance(reply.body, dict) else None
        return isinstance(data, list) and any(isinstance(m, dict) and m.get("id") == self.model for m in data)


def create(ctx: ProviderContext, transport: Transport | None = None) -> OpenAICompatibleText:
    return OpenAICompatibleText(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TEXT, NAME, ConnectionMode.HOSTED, create, settings_schema=OpenAICompatibleSettings)
