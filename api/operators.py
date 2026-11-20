"""Which tenants an operator may see on the console. Owner-side only.

`operator_member` (migration 0035) links identities to operators; it has RLS enabled with no
policy and no grant to the app role, like `operator` and `identity`. The tenant set always comes
from the signed-in identity, never from the request.

`OwnerOperatorDirectory` connects with the owner URL. It reads `operator_member` and `operator`
(not forced, owner reads them) and `tenant` (FORCED row-level security). On the Docker Postgres CI
starts, the owner is a superuser and it works. On Supabase the tenant read returns nothing for a
non-superuser owner, so the console shows no clients (fails closed), the same open question as
`api/directory.py`. Unverified on Supabase.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OperatorTenant:
    tenant_id: str
    tenant_name: str
    operator_id: str
    operator_name: str


class OperatorDirectory(Protocol):
    def tenants_for(self, identity_id: str) -> list[OperatorTenant]: ...


class InMemoryOperatorDirectory:
    def __init__(self) -> None:
        self.members: set[tuple[str, str]] = set()  # (operator_id, identity_id)
        self.operators: dict[str, str] = {}  # operator_id -> name
        self.tenants: dict[str, tuple[str, str]] = {}  # tenant_id -> (name, operator_id)

    def link(self, operator_id: str, operator_name: str, identity_id: str) -> None:
        self.operators[operator_id] = operator_name
        self.members.add((operator_id, identity_id))

    def add_tenant(self, tenant_id: str, name: str, operator_id: str) -> None:
        self.tenants[tenant_id] = (name, operator_id)

    def tenants_for(self, identity_id: str) -> list[OperatorTenant]:
        ops = {o for o, i in self.members if i == identity_id}
        rows = [OperatorTenant(t, name, op, self.operators.get(op, ""))
                for t, (name, op) in self.tenants.items() if op in ops]
        return sorted(rows, key=lambda r: (r.operator_name, r.tenant_name, r.tenant_id))


class OwnerOperatorDirectory:
    def __init__(self, owner_url: str) -> None:
        self.owner_url = owner_url

    def tenants_for(self, identity_id: str) -> list[OperatorTenant]:
        import psycopg

        with psycopg.connect(self.owner_url) as conn:
            rows = conn.execute(
                "SELECT t.id, t.name, o.id, o.name FROM operator_member m "
                "JOIN operator o ON o.id = m.operator_id JOIN tenant t ON t.operator_id = o.id "
                "WHERE m.identity_id = %s ORDER BY o.name, t.name, t.id", (identity_id,)).fetchall()
        return [OperatorTenant(str(r[0]), r[1], str(r[2]), r[3]) for r in rows]


__all__ = ["InMemoryOperatorDirectory", "OperatorDirectory", "OperatorTenant", "OwnerOperatorDirectory"]
