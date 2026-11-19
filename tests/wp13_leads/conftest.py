"""In-memory fixtures for the lead desk: one tenant (region IT), a founder and a seller, a region
policy that allows WhatsApp contact on an enquiry basis, and play A configured. No network."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from api.backend import MemoryBackend
from api.media_store import TenantMediaStore
from thallo.contracts import connection as c
from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import ConnectionMode, Language, UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, RegionPolicy, Tenant
from thallo.leads import Desk


class Verifier:
    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id == "live"


class FakeSender:
    """Records requests; answers like the manual provider (a link) unless told to say sent."""

    def __init__(self, status: str = "manual_link", raises: Exception | None = None) -> None:
        self.status = status
        self.raises = raises
        self.requests: list[c.MessageSendRequest] = []

    def run(self, tenant_id: str, request: c.MessageSendRequest):  # noqa: ANN201
        self.requests.append(request)
        if self.raises:
            raise self.raises
        out = c.MessageSendResult(status=self.status, external_id="x-1" if self.status == "sent" else None,
                                  click_to_chat_url=None if self.status == "sent" else "https://wa.me/393331234567?text=hi")
        res = c.RunResult(output=out, status="done" if self.status == "sent" else "pending_person",
                          provenance=c.Provenance(made_by="provider", provider="fake", mode=ConnectionMode.MANUAL,
                                                  made_at=FAKE_EPOCH),
                          cost_eur=Decimal(0))
        return SimpleNamespace(result=res)


@dataclass
class World:
    clock: FakeClock
    backend: MemoryBackend
    tenant: Tenant
    founder: AppUser
    seller: AppUser
    verifier: Verifier = field(default_factory=Verifier)

    def human(self, user: AppUser, session: str = "live") -> HumanActor:
        return HumanActor(id=user.id, tenant_id=self.tenant.id, session_id=session)

    def desk(self, tx) -> Desk:  # noqa: ANN001
        return Desk(tx, self.tenant.id, self.clock)


POLICY_RULES = {"contact": {"whatsapp": {"bases": ["enquiry", "opt_in"], "footer": "Rispondi STOP per non ricevere altri messaggi"},
                            "email": {"bases": ["opt_in"]}}}


@pytest.fixture
def world(tmp_path) -> World:
    clock = FakeClock(FAKE_EPOCH)
    root = tmp_path / "brand"
    root.mkdir()
    backend = MemoryBackend(clock, TenantMediaStore(root))
    now = clock.now()
    tenant = Tenant(id=new_id(clock), created_at=now, operator_id=new_id(clock), name="Moto", slug="moto",
                    default_language=Language.IT, region_code="IT", currency="EUR")
    backend.add_tenant(tenant)
    t = tenant.id
    with backend.tenant(t) as tx:
        founder = tx.insert(AppUser(id=new_id(clock), created_at=now, tenant_id=t, identity_id=new_id(clock),
                                    display_name="Founder", role=UserRole.FOUNDER))
        seller = tx.insert(AppUser(id=new_id(clock), created_at=now, tenant_id=t, identity_id=new_id(clock),
                                   display_name="Seller", role=UserRole.SELLER))
        tx.insert(RegionPolicy(id=new_id(clock), created_at=now, tenant_id=t, region_code="IT", version=1,
                               rules=POLICY_RULES, effective_from=now - timedelta(days=1)))
        Desk(tx, t, clock).configure("A", service_area=["Bologna", "Imola"], stock=["Leoncino 500"],
                                     response_minutes=30)
    return World(clock, backend, tenant, founder, seller)
