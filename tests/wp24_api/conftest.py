"""WP-24 fixtures: the WP-09 world (memory and Postgres), with owner-side token and operator stores
on Postgres, and `both` (a member of Alpha and Beta) linked to Alpha's operator only."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.context import CSRF_HEADER
from api.operators import InMemoryOperatorDirectory, OwnerOperatorDirectory
from api.token_store import InMemoryTokenStore, OwnerTokenStore
from tests.wp09_api.conftest import World, pg_urls, world  # noqa: F401  (fixtures)


@dataclass
class W:
    world: World
    both_identity: str

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self.world, name)

    def token(self, email: str, tenant_id: str, scopes=("read",), days: int = 30) -> str:  # noqa: ANN001
        c, csrf = self.world.login(email, tenant_id)
        r = c.post("/api/tokens", json={"name": "agent", "scopes": list(scopes), "days": days},
                   headers={CSRF_HEADER: csrf})
        assert r.status_code == 201, r.text
        return r.json()["token"]


@pytest.fixture
def w(world) -> W:  # noqa: F811
    both = world.svc.directory.find_identity(world.both_email).id
    op_a = world.a.tenant.operator_id
    if world.kind == "memory":
        ops = InMemoryOperatorDirectory()
        for shop in (world.a, world.b):
            ops.add_tenant(shop.tenant.id, shop.tenant.name, shop.tenant.operator_id)
        ops.link(op_a, "op Alpha", both)
        world.svc.operators = ops
        world.svc.tokens = InMemoryTokenStore()
    else:
        import psycopg

        with psycopg.connect(world.urls["admin"]) as conn:
            conn.execute("INSERT INTO operator_member (operator_id, identity_id, created_at) VALUES (%s, %s, %s)",
                         (op_a, both, world.clock.now()))
        world.svc.operators = OwnerOperatorDirectory(world.urls["admin"])
        world.svc.tokens = OwnerTokenStore(world.urls["admin"])
    return W(world=world, both_identity=both)


def auth(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}
