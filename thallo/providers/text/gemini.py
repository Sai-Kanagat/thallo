"""Gemini, through the Generative Language REST API over plain HTTPS (no SDK is a dependency).

    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
    header: x-goog-api-key
    body:   systemInstruction={parts: [{text}]}, contents=[{role: user, parts: [{text}]}],
            generationConfig={maxOutputTokens}
    reply:  candidates=[{content: {parts: [{text} | {text, thought: true}]}, finishReason}],
            usageMetadata={promptTokenCount, candidatesTokenCount, thoughtsTokenCount}, responseId,
            modelVersion

Shape from Google's REST reference as of Nov 2026; the test fixtures are written by hand from it,
not captured. Thinking tokens are billed as output, so they are counted as output here.
"""
from __future__ import annotations

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

NAME = "gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
BLOCKED = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"})


class GeminiSettings(PricedSettings):
    base_url: str = DEFAULT_BASE_URL

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        return check_https(v)


class GeminiText(HttpTextProvider):
    name = NAME
    mode = ConnectionMode.HOSTED
    licence_note = "Google Gemini API terms; prompts and outputs leave this system"
    Settings = GeminiSettings

    def auth(self, secret: Secret) -> Auth:
        return Auth("x-goog-api-key", secret)

    def _base(self) -> str:
        assert isinstance(self.settings, GeminiSettings)
        return f"{self.settings.base_url}/v1beta/models/{self.model}"

    def build_call(self, request: c.TextRequest, auth: Auth | None) -> HttpCall:
        assert self.settings is not None
        config: dict = {"maxOutputTokens": request.max_output_tokens}
        if request.output_schema is not None:
            config["responseMimeType"] = "application/json"
        body = {
            "systemInstruction": {"parts": [{"text": system_text(request)}]},
            "contents": [{"role": "user", "parts": [{"text": user_text(request)}]}],
            "generationConfig": config,
        }
        return HttpCall("POST", f"{self._base()}:generateContent", {}, body, auth, self.settings.timeout_s)

    def parse(self, reply: HttpReply) -> Parsed:
        b = reply.body
        need(isinstance(b, dict), NAME, "body")
        feedback = b.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise TextProviderError("gemini blocked the prompt")
        cands = b.get("candidates")
        need(isinstance(cands, list) and bool(cands) and isinstance(cands[0], dict), NAME, "candidates")
        first = cands[0]
        reason = first.get("finishReason")
        if reason in BLOCKED:
            raise TextProviderError(f"gemini stopped the output ({reason})")
        if reason == "MAX_TOKENS":
            raise TextProviderError("gemini stopped at maxOutputTokens; the output is cut")
        parts = (first.get("content") or {}).get("parts")
        need(isinstance(parts, list), NAME, "parts")
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")]
        need(bool(texts), NAME, "no text part")
        usage = b.get("usageMetadata") if isinstance(b.get("usageMetadata"), dict) else {}
        out = tokens(usage.get("candidatesTokenCount")) + tokens(usage.get("thoughtsTokenCount"))
        return Parsed("".join(texts).strip(), tokens(usage.get("promptTokenCount")), out,
                      b.get("responseId") if isinstance(b.get("responseId"), str) else None,
                      b.get("modelVersion") if isinstance(b.get("modelVersion"), str) else None)

    def health_call(self, auth: Auth | None) -> HttpCall:
        return HttpCall("GET", self._base(), {}, None, auth, 15.0)

    def health_ok(self, reply: HttpReply) -> bool:
        return isinstance(reply.body, dict) and reply.body.get("name") == f"models/{self.model}"


def create(ctx: ProviderContext, transport: Transport | None = None) -> GeminiText:
    return GeminiText(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.TEXT, NAME, ConnectionMode.HOSTED, create, settings_schema=GeminiSettings)
