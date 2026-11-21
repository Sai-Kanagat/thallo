"""WP-33 on Postgres: import into two tenants, each verifies on its own, expiry trigger, append-only."""
from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from tests.wp01_store.conftest import URL, make_tenant
from tests.wp33_packs.conftest import DAY, LANGS, FakeVerifier, library, signer, trusting
from thallo.contracts.actors import HumanActor
from thallo.contracts.enums import UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Claim, Identity
from thallo.contracts.states import ClaimState
from thallo.extensions.packs import (
    PackError,
    export_pack,
    import_pack,
    imported_digests,
    link_for,
    links,
    to_bytes,
    verify_pack_claim,
    write_import,
)
from thallo.store.adapters.claims import PgClaimRepository
from thallo.truth import verify_claim

pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")


def user(database, admin, tenant) -> HumanActor:
    clock = database.clock
    ident = Identity(id=new_id(clock), created_at=clock.now(), email=f"{new_id(clock)}@example.invalid",
                     display_name="Owner")
    admin.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                  (ident.id, ident.created_at, ident.email, ident.display_name))
    with database.tenant(tenant.id) as tx:
        u = AppUser(id=new_id(clock), created_at=clock.now(), tenant_id=tenant.id, identity_id=ident.id,
                    display_name="Owner", role=UserRole.APPROVER)
        tx.insert(u)
    return HumanActor(id=u.id, tenant_id=tenant.id, session_id="live")


def a_pack(database, s):
    claims, sources = library(clock_at=database.clock.now())
    return to_bytes(export_pack(claims=claims, sources=sources, codes=["C-0001", "C-0004"], pack_id="maker-x",
                                title="Model X", publisher="Maker", expires_at=database.clock.now() + 50 * DAY,
                                languages=LANGS, clock=database.clock, signer=s))


def imported(database, tenant, actor, data, s):
    with database.tenant(tenant.id) as tx:
        codes = [c.code for c in tx.list_all(Claim)]
        out = import_pack(data, trusted=trusting(s), tenant_id=tenant.id, existing_codes=codes,
                          tenant_languages=LANGS, clock=database.clock, actor=actor,
                          imported_digests=imported_digests(tx))
        write_import(tx, out)
    return out


def test_two_tenants_each_verify_and_expiry_is_enforced(database, admin):
    s = signer()
    data = a_pack(database, s)
    one, two = make_tenant(database, "Dealer one"), make_tenant(database, "Dealer two")
    h1, h2 = user(database, admin, one), user(database, admin, two)
    out1, out2 = imported(database, one, h1, data, s), imported(database, two, h2, data, s)

    with database.tenant(one.id) as tx:
        repo = PgClaimRepository(tx)
        claim = repo.latest(one.id, out1.claims[0].code)
        assert claim.state is ClaimState.CANDIDATE
        verified, _ = verify_pack_claim(claim, link_for(tx, claim.id), h1, verifier=FakeVerifier(),
                                        clock=database.clock, repo=repo, tenant_languages=LANGS)
        repo.save(verified)
        assert len(links(tx)) == 2
    with database.tenant(two.id) as tx:
        assert all(c.state is ClaimState.CANDIDATE for c in tx.list_all(Claim))  # one's verify did not leak
        assert {lk.claim_id for lk in links(tx)} == {c.id for c in out2.claims}

    # a generic verify past the pack expiry is refused by the database
    with pytest.raises(psycopg.errors.RaiseException, match="pack expiry"), database.tenant(two.id) as tx:
        repo = PgClaimRepository(tx)
        claim = repo.latest(two.id, out2.claims[1].code)
        late, _ = verify_claim(claim, h2, verifier=FakeVerifier(), clock=database.clock, repo=repo,
                               expires_at=database.clock.now() + timedelta(days=80))
        repo.save(late)

    with pytest.raises(PackError, match="already"):
        imported(database, one, h1, data, s)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(one.id) as tx:
        tx.execute("UPDATE claim_pack_import SET pack_expires_at = pack_expires_at + interval '1 year'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(one.id) as tx:
        tx.execute("DELETE FROM claim_pack_import")
