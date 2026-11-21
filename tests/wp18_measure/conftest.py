"""WP-18 fixtures: one tenant on the in-memory backend, a founder, a fake clock. No network."""
from __future__ import annotations

import socket
from dataclasses import dataclass

import pytest

from api.backend import MemoryBackend
from api.media_store import TenantMediaStore
from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Language, UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Tenant


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("outside call attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


class Verifier:
    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id == "live"


@dataclass
class Mem:
    clock: FakeClock
    backend: MemoryBackend
    tenant: Tenant
    founder: AppUser

    def human(self, session: str = "live") -> HumanActor:
        return HumanActor(id=self.founder.id, tenant_id=self.tenant.id, session_id=session)


@pytest.fixture
def mem(tmp_path) -> Mem:
    clock = FakeClock(FAKE_EPOCH)
    backend = MemoryBackend(clock, TenantMediaStore(tmp_path))
    now = clock.now()
    tenant = Tenant(id=new_id(clock), created_at=now, operator_id=new_id(clock), name="Bakery", slug="bakery",
                    default_language=Language.EN, region_code="IT", currency="EUR")
    backend.add_tenant(tenant)
    with backend.tenant(tenant.id) as tx:
        founder = tx.insert(AppUser(id=new_id(clock), created_at=now, tenant_id=tenant.id,
                                    identity_id=new_id(clock), display_name="Founder", role=UserRole.FOUNDER))
    clock.advance(days=120)  # room for pieces sent "days ago"
    return Mem(clock, backend, tenant, founder)


@pytest.fixture
def verifier():
    return Verifier()
