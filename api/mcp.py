"""The MCP server (WP-24): JSON-RPC 2.0 over HTTP POST, JSON responses (no streaming, no SDK).

  POST /api/mcp     Authorization: Bearer thl_...   methods: initialize, ping, tools/list, tools/call

Every tool is one row of `api.access.ROUTES` with the read or draft class, so the tool list and
the API can never drift apart. A tool call is replayed as an ordinary API request into this same
app with the same bearer token, so it passes the same scope check, rate limit, role check and
per-call audit as a direct API call. Approval, claim verification, sending, publishing and spend
signing have no tool; asking for one by name is refused.

The MCP request itself is authenticated before anything is listed, and audited.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.access import tools as access_tools
from api.context import audit_token_call, bearer, resolve_token, services
from api.errors import ApiError

router = APIRouter(prefix="/api")

SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "thallo", "version": "0.1.0"}
_PARAM = re.compile(r"\{([a-z_]+)\}")
_SEGMENT = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_BODY = 64_000


def _schema(template: str, method: str) -> dict:
    params = _PARAM.findall(template)
    props: dict[str, Any] = {p: {"type": "string"} for p in params}
    if method == "GET":
        props["query"] = {"type": "object", "description": "Query string parameters", "additionalProperties": True}
    else:
        props["body"] = {"type": "object", "description": "JSON body of the API call", "additionalProperties": True}
    return {"type": "object", "properties": props, "required": params, "additionalProperties": False}


def catalogue() -> dict[str, tuple[str, str, dict]]:
    """name -> (method, template, tool definition)."""
    out = {}
    for method, template, access, tool in access_tools():
        out[tool.name] = (method, template, {
            "name": tool.name, "description": f"{tool.description} ({access.value})",
            "inputSchema": _schema(template, method),
            "annotations": {"readOnlyHint": access.value == "read", "destructiveHint": False},
        })
    return out


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


async def _call(request: Request, raw: str, name: str, args: Any) -> dict:
    entry = catalogue().get(name)
    if entry is None:
        return _tool_error("No such tool. Approving, sending, publishing, verifying claims and signing spend "
                           "are done by a person in the app, never through MCP.")
    method, template, _ = entry
    if not isinstance(args, dict):
        return _tool_error("arguments must be an object")
    path = template
    for p in _PARAM.findall(template):
        v = args.get(p)
        if not isinstance(v, str) or not _SEGMENT.match(v):
            return _tool_error(f"argument {p} is missing or not valid")
        path = path.replace("{" + p + "}", v)
    extra = set(args) - set(_PARAM.findall(template)) - {"query", "body"}
    if extra:
        return _tool_error("unknown arguments: " + ", ".join(sorted(extra))[:200])
    query = args.get("query") or {}
    body = args.get("body")
    if not isinstance(query, dict) or any(not isinstance(v, str | int) or isinstance(v, bool) for v in query.values()):
        return _tool_error("query must be an object of strings or numbers")
    if body is not None and (not isinstance(body, dict) or len(json.dumps(body)) > MAX_BODY):
        return _tool_error("body must be a JSON object under 64 KB")
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://thallo.internal") as client:
        r = await client.request(method, path, params={k: str(v) for k, v in query.items()},
                                 json=body if method != "GET" else None,
                                 headers={"authorization": f"Bearer {raw}"})
    try:
        data = r.json()
    except ValueError:
        data = {"error": "not_json"}
    text = json.dumps(data, ensure_ascii=False)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": r.status_code >= 400}
    if isinstance(data, dict):
        result["structuredContent"] = data
    return result


@router.post("/mcp")
async def mcp(request: Request) -> JSONResponse:
    raw = bearer(request)
    if raw is None:
        raise ApiError(401, "unauthenticated", "an API token is required")
    tok = resolve_token(request, raw)
    try:
        msg = await request.json()
    except ValueError:
        return JSONResponse(_err(None, -32700, "parse error"), status_code=400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return JSONResponse(_err(None, -32600, "invalid request"), status_code=400)
    rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    svc = services(request)
    from api.context import TOKEN_RATE, TOKEN_WINDOW

    if not svc.limits.allow(f"api_token:{tok.id}", TOKEN_RATE, TOKEN_WINDOW):
        audit_token_call(request, tok, "api_token.refused", reason="rate_limited", mcp=method[:60])
        return JSONResponse(_err(rid, -32000, "rate limited"), status_code=429)
    audit_token_call(request, tok, "api_token.mcp", mcp=method[:60],
                     tool=str(params.get("name"))[:60] if isinstance(params, dict) and method == "tools/call" else None)
    if "id" not in msg:  # a notification: no reply
        return JSONResponse(None, status_code=202)
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if asked in SUPPORTED else SUPPORTED[0]
        return JSONResponse(_ok(rid, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                      "serverInfo": SERVER,
                                      "instructions": "Read and draft only. A person approves, sends and spends."}))
    if method == "ping":
        return JSONResponse(_ok(rid, {}))
    if method == "tools/list":
        scopes = {s.value for s in tok.scopes}
        listed = [d for m, t, d in catalogue().values()
                  if ("read" if d["annotations"]["readOnlyHint"] else "draft") in scopes]
        return JSONResponse(_ok(rid, {"tools": listed}))
    if method == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return JSONResponse(_err(rid, -32602, "invalid params"))
        return JSONResponse(_ok(rid, await _call(request, raw, params["name"], params.get("arguments") or {})))
    return JSONResponse(_err(rid, -32601, "method not found"))


__all__ = ["SUPPORTED", "catalogue", "router"]
