"""What an API token may call. One table, default deny.

Every route is classified by (method, route template):

  READ         a token with the read scope may call it
  DRAFT        a token with the draft scope may call it: it changes a draft and nothing else
  HUMAN        human-only (approve, reject, send, book, record an outcome, raise trust, reset a
               breaker, mark the hand audit). Refused to every token here, and refused again by
               the SessionVerifier inside the call, which accepts only interactive sessions.
  INTERACTIVE  a person in a browser only: sign-in, tokens, the console, connection secrets and
               settings, provider calls that cost money, voice recordings, plan and model changes

A route missing from the table is INTERACTIVE. The same table drives the scope check in
`api.context.member`, the MCP tool list in `api.mcp` and the tests.

Draft routes are kept to the ones whose writes carry no human provenance: a review edit is left
out because it records an `edit` row as the signed-in person's and feeds trust evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from api.token_store import TokenScope


class Access(StrEnum):
    READ = "read"
    DRAFT = "draft"
    HUMAN = "human"
    INTERACTIVE = "interactive"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str


R, D, H, IA = Access.READ, Access.DRAFT, Access.HUMAN, Access.INTERACTIVE

ROUTES: dict[tuple[str, str], tuple[Access, Tool | None]] = {
    # sign-in, tokens, console: a person only
    ("GET", "/api/session"): (IA, None),
    ("POST", "/api/auth/request-code"): (IA, None),
    ("POST", "/api/auth/verify"): (IA, None),
    ("GET", "/api/auth/tenants"): (IA, None),
    ("POST", "/api/auth/tenant"): (IA, None),
    ("POST", "/api/auth/logout"): (IA, None),
    ("GET", "/api/tokens"): (IA, None),
    ("POST", "/api/tokens"): (IA, None),
    ("POST", "/api/tokens/{token_id}/revoke"): (IA, None),
    ("GET", "/api/console"): (IA, None),
    ("POST", "/api/mcp"): (IA, None),  # authenticates itself; each tool call goes through this table
    # review
    ("GET", "/api/review/bundles"): (R, Tool("review_list", "Bundles waiting in the review queue.")),
    ("GET", "/api/review/bundles/{bundle_id}"): (R, Tool("review_get", "One bundle with its variants and checks.")),
    ("POST", "/api/review/bundles/{bundle_id}/approve"): (H, None),
    ("POST", "/api/review/variants/{variant_id}/edit"): (IA, None),
    ("POST", "/api/review/bundles/{bundle_id}/reject"): (H, None),
    ("POST", "/api/review/bundles/{bundle_id}/withdraw"): (H, None),
    ("GET", "/api/media/{media_id}"): (R, None),  # bytes: API only, not an MCP tool
    # plan
    ("GET", "/api/plan"): (R, Tool("plan_get", "This week's plan slots and pillar shares.")),
    ("POST", "/api/plan/slots/{slot_id}/kill"): (IA, None),
    ("POST", "/api/plan/slots/{slot_id}/move"): (IA, None),
    # connections: secrets and provider calls stay with a person
    ("GET", "/api/connections"): (R, Tool("connections_list", "Connections and their health.")),
    ("GET", "/api/connections/providers"): (R, Tool("providers_list", "Providers available per capability.")),
    ("POST", "/api/connections"): (IA, None),
    ("POST", "/api/connections/{connection_id}/enabled"): (IA, None),
    ("POST", "/api/connections/order"): (IA, None),
    ("POST", "/api/connections/{connection_id}/secret"): (IA, None),
    ("DELETE", "/api/connections/{connection_id}"): (IA, None),
    ("POST", "/api/connections/{connection_id}/test"): (IA, None),
    # growth
    ("GET", "/api/growth"): (R, Tool("growth_get", "The growth model, stages and plays.")),
    ("POST", "/api/growth/preview"): (D, Tool("growth_preview",
                                                 "Preview stage guesses from owner answers; saves nothing.")),
    ("POST", "/api/growth/model"): (IA, None),
    ("POST", "/api/growth/plays/{code}"): (IA, None),
    # leads
    ("GET", "/api/leads"): (R, Tool("leads_list", "The lead grid: fit, heat, route and clock.")),
    ("GET", "/api/leads/{lead_id}"): (R, Tool("lead_get", "One lead with questions, fit table and drafts.")),
    ("POST", "/api/leads/{lead_id}/answers"): (D, Tool("lead_answers", "Record qualifying answers on a lead.")),
    ("POST", "/api/leads/{lead_id}/book"): (H, None),
    ("POST", "/api/leads/{lead_id}/outcome"): (H, None),
    ("POST", "/api/leads/messages/{message_id}/edit"): (D, Tool("lead_message_edit",
                                                                     "Edit a draft reply. Never sends.")),
    ("POST", "/api/leads/messages/{message_id}/discard"): (D, Tool("lead_message_discard", "Discard a draft reply.")),
    ("POST", "/api/leads/messages/{message_id}/send"): (H, None),
    ("POST", "/api/leads/messages/{message_id}/sent"): (H, None),
    # voice capture: the owner's own voice and photos
    ("GET", "/api/voice"): (R, Tool("voice_get", "Voice interview questions, answers and shot requests.")),
    ("PUT", "/api/voice/questions/{question_id}"): (IA, None),
    ("POST", "/api/voice/questions/{question_id}/recording"): (IA, None),
    ("POST", "/api/voice/interview/recording"): (IA, None),
    ("POST", "/api/voice/questions/{question_id}/typed"): (IA, None),
    ("POST", "/api/voice/answers/{answer_id}/transcribe"): (IA, None),
    ("POST", "/api/voice/answers/{answer_id}/transcript"): (IA, None),
    ("POST", "/api/voice/shots"): (IA, None),
    ("POST", "/api/voice/shots/{shot_id}/file"): (IA, None),
    ("POST", "/api/voice/shots/{shot_id}/drop"): (IA, None),
    # trust
    ("GET", "/api/trust"): (R, Tool("trust_get", "Trust ladder per task type, breaker trips, open incidents.")),
    ("POST", "/api/trust/{task_type}/raise"): (H, None),
    ("POST", "/api/trust/breakers/{trip_id}/reset"): (H, None),
    ("GET", "/api/trust/audit"): (IA, None),  # tops up the weekly sample on read
    ("POST", "/api/trust/audit/{sentence_id}"): (H, None),
    # public redirect, no auth at all
    ("GET", "/r/{slug}"): (IA, None),
}

_ALLOWED = {TokenScope.READ: Access.READ, TokenScope.DRAFT: Access.DRAFT}


def app_routes(app) -> list[tuple[frozenset[str], str]]:  # noqa: ANN001
    """(methods, full template) for every route, in router order, through included routers
    (this FastAPI keeps an included router as one entry rather than copying its routes)."""
    out: list[tuple[frozenset[str], str]] = []

    def walk(routes, prefix: str) -> None:  # noqa: ANN001
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                ctx = getattr(r, "include_context", None)
                walk(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            elif getattr(r, "path", None) is not None and getattr(r, "methods", None):
                out.append((frozenset(r.methods), prefix + r.path))

    walk(app.router.routes, "")
    return out


def classify(method: str, template: str) -> Access:
    entry = ROUTES.get((method.upper(), template))
    return entry[0] if entry is not None else Access.INTERACTIVE


def token_may(scopes: frozenset[TokenScope], method: str, template: str) -> bool:
    access = classify(method, template)
    return any(_ALLOWED.get(s) is access for s in scopes)


def tools() -> list[tuple[str, str, Access, Tool]]:
    """(method, template, access, tool) for every route an MCP client may call."""
    return [(m, p, a, t) for (m, p), (a, t) in ROUTES.items() if t is not None and a in (Access.READ, Access.DRAFT)]


__all__ = ["ROUTES", "Access", "Tool", "app_routes", "classify", "token_may", "tools"]
