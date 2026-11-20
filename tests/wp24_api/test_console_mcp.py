"""The operator console (one operator's tenants only) and the MCP server (read and draft only)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from api.mcp import catalogue
from tests.wp24_api.conftest import auth
from thallo.contracts.enums import Capability, Channel, TextRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import ChannelAccount, CostEntry, Job, PromptVersion
from thallo.trust.records import CostCap


def _seed_numbers(w) -> None:  # noqa: ANN001
    now = w.clock.now()
    t = w.a.tenant.id
    with w.svc.backend.tenant(t) as tx:
        tx.insert(Job(id=new_id(w.clock), created_at=now, tenant_id=t, kind="render", run_at=now - timedelta(hours=1)))
        tx.insert(CostEntry(id=new_id(w.clock), created_at=now, tenant_id=t, capability=Capability.TEXT,
                            provider="mock", cost_eur=Decimal("1.50"), run_ref="r1", occurred_at=now))
        tx.insert(CostCap(id=new_id(w.clock), created_at=now, tenant_id=t, scope="day", provider="*",
                          cap_eur=Decimal("10"), set_by="founder", set_at=now))
        tx.insert(PromptVersion(id=new_id(w.clock), created_at=now, tenant_id=t, name="draft", version=2,
                                role=TextRole.DRAFT, body_sha256="a" * 64, model_id="mock", eval_passed=True,
                                activated_at=now))
        tx.insert(ChannelAccount(id=new_id(w.clock), created_at=now, tenant_id=t, channel=Channel.INSTAGRAM,
                                 handle="alpha", token_expires_at=now + timedelta(days=5)))


def test_console_shows_only_the_operators_tenants(w) -> None:  # noqa: ANN001
    _seed_numbers(w)
    c, _ = w.login(w.both_email, w.a.tenant.id)
    r = c.get("/api/console")
    assert r.status_code == 200, r.text
    tenants = r.json()["tenants"]
    assert [t["tenant_id"] for t in tenants] == [w.a.tenant.id]  # both is in Beta too, but Beta is another operator's
    a = tenants[0]
    assert a["queue"]["status"] == "behind" and a["queue"]["due_waiting"] == 1
    assert a["cost"]["today_eur"] == "1.50" and a["cost"]["day_cap_eur"] == "10.00" and a["cost"]["day_pct"] == 15
    assert a["prompts"][0]["name"] == "draft" and a["prompts"][0]["eval_passed"] is True
    assert a["expiries"][0]["kind"] == "channel" and a["expiries"][0]["days_left"] == 5


def test_console_without_cap_says_so(w) -> None:  # noqa: ANN001
    c, _ = w.login(w.both_email, w.a.tenant.id)
    cost = c.get("/api/console").json()["tenants"][0]["cost"]
    assert cost["cap_note"] == "no cap set" and cost["day_pct"] is None


def test_console_refuses_non_operators_and_tokens(w) -> None:  # noqa: ANN001
    c, _ = w.login(w.a.founder_email, w.a.tenant.id)
    assert c.get("/api/console").status_code == 403
    assert w.client().get("/api/console").status_code == 401
    tok = w.token(w.a.founder_email, w.a.tenant.id)
    assert w.client().get("/api/console", headers=auth(tok)).status_code == 403


def _rpc(c, tok: str, method: str, params: dict | None = None, rid: int = 1):  # noqa: ANN001, ANN202
    return c.post("/api/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                  headers=auth(tok))


def test_mcp_lists_and_calls_read_tools(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id)
    c = w.client()
    init = _rpc(c, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "1"}}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    names = {t["name"] for t in _rpc(c, tok, "tools/list").json()["result"]["tools"]}
    assert "review_list" in names and "lead_message_edit" not in names  # read scope only
    assert not any("approve" in n or "send" in n or "publish" in n for n in catalogue())
    res = _rpc(c, tok, "tools/call", {"name": "review_list", "arguments": {}}).json()["result"]
    assert res["isError"] is False
    assert [b["bundle_id"] for b in res["structuredContent"]["bundles"]] == [w.a.bundle_id]
    actions = [e.action for e in _audit(w)]
    assert "api_token.mcp" in actions and "api_token.call" in actions


def test_mcp_refuses_what_tokens_cannot_do(w) -> None:  # noqa: ANN001
    tok = w.token(w.a.founder_email, w.a.tenant.id, scopes=("read",))
    c = w.client()
    res = _rpc(c, tok, "tools/call", {"name": "review_approve", "arguments": {"bundle_id": w.a.bundle_id}}).json()
    assert res["result"]["isError"] is True
    res = _rpc(c, tok, "tools/call", {"name": "lead_message_edit",
                                      "arguments": {"message_id": w.a.bundle_id, "body": {"body": "x"}}}).json()
    assert res["result"]["isError"] is True and "token_refused" in res["result"]["content"][0]["text"]
    res = _rpc(c, tok, "tools/call", {"name": "review_get", "arguments": {"bundle_id": "../../auth/tenant"}}).json()
    assert res["result"]["isError"] is True
    assert c.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def _audit(w) -> list:  # noqa: ANN001
    with w.svc.backend.tenant(w.a.tenant.id) as tx:
        return list(tx.audit.store.entries(w.a.tenant.id))
