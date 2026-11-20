"""Shared helpers for WP-27. No keys, no network: the writer is a scripted double."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.wp01_store.conftest import admin, clock, database, urls  # noqa: F401
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import ClaimKind, Language, SourceKind, UserRole
from thallo.contracts.models import AppUser, Claim, Source, Tenant
from thallo.contracts.states import ClaimState
from thallo.contracts.testing.samples import T, _id
from thallo.extensions.pages import QueryRow, SitePage, WriterReply
from thallo.extensions.pages.queries import cluster

GOOD = """# How much does a boiler service cost in Bologna

> A boiler service in Bologna costs {{C-0001}} and the visit takes about {{C-0002}} at your home.

## What the service includes

We check the burner, clean the flue and test the pressure. You get a written report.

| Item ;; Detail
| Price ;; from {{C-0001}} per visit
| Time ;; about {{C-0002}} on site

? Do I need to be home
Yes, someone must let the engineer in.

See [our boiler services](/services/boiler) and [how to book](/contact).
"""

SITE = [SitePage("/services/boiler", "Boiler repair and installation"), SitePage("/contact", "Contact us"),
        SitePage("/about", "About the company")]
ROWS = [QueryRow("boiler service cost bologna", 400, 10, 8.2),
        QueryRow("how much does a boiler service cost", 180, 3, 11.0),
        QueryRow("boiler service price bologna", 90, 1, 14.0)]


def now_clock() -> FakeClock:
    return FakeClock(FAKE_EPOCH)


def make_claims(tenant_id: str = T, base: int = 1):
    now = FAKE_EPOCH
    src = {_id(base + 50): Source(id=_id(base + 50), created_at=now, tenant_id=tenant_id, kind=SourceKind.DOCUMENT,
                                  title="Price list", uri="https://example.invalid/prices", captured_at=now)}
    common = dict(created_at=now, tenant_id=tenant_id, state=ClaimState.VERIFIED, source_id=_id(base + 50),
                  verified_by=_id(base + 70), verified_at=now - timedelta(days=3),
                  expires_at=now + timedelta(days=30))
    claims = {
        "C-0001": Claim(id=_id(base), code="C-0001", kind=ClaimKind.PRICE, value="45", unit="EUR", **common),
        "C-0002": Claim(id=_id(base + 1), code="C-0002", kind=ClaimKind.NUMBER, value="1", unit="hour", **common),
    }
    return claims, src


def make_tenant_row(tenant_id: str = T) -> Tenant:
    return Tenant(id=tenant_id, created_at=FAKE_EPOCH, operator_id=_id(99), name="Caldaie", slug="caldaie",
                  default_language=Language.EN, region_code="IT", currency="EUR")


def make_reviewer(tenant_id: str = T, user_id: str | None = None, role: UserRole = UserRole.APPROVER) -> AppUser:
    return AppUser(id=user_id or _id(80), created_at=FAKE_EPOCH, tenant_id=tenant_id, identity_id=_id(81),
                   display_name="Sai", role=role)


def the_cluster():
    return cluster(ROWS)[0]


@dataclass
class Scripted:
    """Returns the queued texts in order and records what it was given."""

    texts: list[str]
    cost: Decimal = Decimal("0.40")
    calls: list[dict] = field(default_factory=list)

    def write(self, *, ref, instructions, data, cap_eur):
        self.calls.append({"ref": ref, "instructions": instructions, "data": list(data), "cap": cap_eur})
        if not self.texts:
            raise AssertionError("no scripted reply left")
        return WriterReply(self.texts.pop(0), "fake", "fake-model", self.cost, 1000, billed=True)


@pytest.fixture
def no_links():
    return lambda url: None
