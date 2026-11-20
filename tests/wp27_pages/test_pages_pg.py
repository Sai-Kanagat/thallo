"""WP-27 on Postgres: the outcome round trips, one page per cluster, tenant isolation, append-only."""
from __future__ import annotations

import psycopg
import pytest

from tests.wp01_store.conftest import URL, make_tenant
from tests.wp27_pages.conftest import GOOD, SITE, Scripted, make_claims, the_cluster
from thallo.contracts.enums import UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Identity
from thallo.extensions.pages import PageInputs, draft_page, existing, write_page

pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")


def setup(database, tenant):
    clock = database.clock
    with database.admin() as a:
        ident = Identity(id=new_id(clock), created_at=clock.now(), email=f"{new_id(clock)}@example.invalid")
        a.insert(ident)
    with database.tenant(tenant.id) as tx:
        user = AppUser(id=new_id(clock), created_at=clock.now(), tenant_id=tenant.id, identity_id=ident.id,
                       display_name="Reviewer", role=UserRole.APPROVER)
        tx.insert(user)
        claims, src = make_claims(tenant.id, base=200)
        for s in src.values():
            tx.insert(s)
        for c in claims.values():
            tx.insert(c)
    return user, claims, src


def run(database, tenant, user, claims, src, slug="/boiler-service-cost"):
    inputs = PageInputs(tenant=tenant, cluster=the_cluster(), slug=slug, reviewer=user, claims=claims, sources=src,
                        site_pages=SITE, clock=database.clock, resolver=lambda u: None, non_links=frozenset())
    return draft_page(writer=Scripted([GOOD]), inputs=inputs)


def test_round_trip_one_page_per_cluster_and_isolation(database):
    tenant = make_tenant(database, "Pages")
    user, claims, src = setup(database, tenant)
    out = run(database, tenant, user, claims, src)
    with database.tenant(tenant.id) as tx:
        write_page(tx, out)
        keys, slugs = existing(tx)
        assert keys == {out.page_brief["cluster_key"]} and slugs == {"/boiler-service-cost"}
    again = run(database, tenant, user, claims, src, slug="/another-slug")
    with pytest.raises(psycopg.errors.UniqueViolation), database.tenant(tenant.id) as tx:
        write_page(tx, again)
    other = make_tenant(database, "Other pages")
    with database.tenant(other.id) as tx:
        assert existing(tx) == (set(), set())
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("UPDATE page_brief SET cluster_key = 'x'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("DELETE FROM page_brief")
