"""Who may sign in, and into which tenants. Owner-side only: the app role has no access to
`identity` (migration 0012), so these lookups never run as the app role.

`OwnerDirectory` connects with the owner URL. It reads `identity` (RLS enabled, not forced, no
policy: the owner reads it) and then the identity's `app_user` rows across tenants. `app_user` and
`tenant` have FORCED row-level security, so that second read returns rows only for an owner that
is allowed past the policy. On the Docker Postgres CI starts, the owner is a superuser and it
works. On Supabase the owner is not a superuser, so it returns nothing and nobody can sign in
(fails closed) until the lead decides how login reads memberships (for example a narrow login
role with its own policy, the pattern of `cortex_worker` in 0012). Unverified on Supabase.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from thallo.contracts.enums import UserRole
from thallo.contracts.models import AppUser, Identity, Tenant


@dataclass(frozen=True)
class Membership:
    app_user_id: str
    tenant_id: str
    tenant_name: str
    role: UserRole
    display_name: str


class IdentityDirectory(Protocol):
    def find_identity(self, email: str) -> Identity | None: ...
    def memberships(self, identity_id: str) -> list[Membership]: ...


def normalise_email(raw: str) -> str | None:
    email = raw.strip().lower()
    local, at, domain = email.partition("@")
    if not at or not local or "." not in domain or len(email) > 254 or any(c.isspace() for c in email):
        return None
    return email


class InMemoryDirectory:
    def __init__(self) -> None:
        self.identities: dict[str, Identity] = {}
        self.users: list[AppUser] = []
        self.tenants: dict[str, Tenant] = {}

    def add(self, identity: Identity, tenant: Tenant | None = None, users: Iterable[AppUser] = ()) -> None:
        self.identities[identity.id] = identity
        if tenant is not None:
            self.tenants[tenant.id] = tenant
        self.users.extend(users)

    def find_identity(self, email: str) -> Identity | None:
        return next((i for i in self.identities.values() if i.email.lower() == email), None)

    def memberships(self, identity_id: str) -> list[Membership]:
        return sorted((Membership(u.id, u.tenant_id, self.tenants[u.tenant_id].name, u.role, u.display_name)
                       for u in self.users if u.identity_id == identity_id and u.tenant_id in self.tenants),
                      key=lambda m: (m.tenant_name, m.tenant_id))


class OwnerDirectory:
    def __init__(self, owner_url: str) -> None:
        self.owner_url = owner_url

    def find_identity(self, email: str) -> Identity | None:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.owner_url) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, created_at, email, display_name FROM identity WHERE lower(email) = %s "
                        "ORDER BY id LIMIT 1", (email,))
            row = cur.fetchone()
        return Identity.model_validate({**row, "id": str(row["id"])}) if row else None

    def memberships(self, identity_id: str) -> list[Membership]:
        import psycopg

        with psycopg.connect(self.owner_url) as conn:
            rows = conn.execute(
                "SELECT u.id, u.tenant_id, t.name, u.role, u.display_name FROM app_user u "
                "JOIN tenant t ON t.id = u.tenant_id WHERE u.identity_id = %s ORDER BY t.name, t.id",
                (identity_id,)).fetchall()
        return [Membership(str(r[0]), str(r[1]), r[2], UserRole(r[3]), r[4]) for r in rows]


__all__ = ["IdentityDirectory", "InMemoryDirectory", "Membership", "OwnerDirectory", "normalise_email"]
