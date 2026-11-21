"""Migration 0037 on Postgres: round trip, tenant isolation, append-only for the app role."""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from tests.wp01_store.conftest import URL, admin, clock, database, make_tenant, urls  # noqa: F401
from thallo.contracts.actors import HumanActor
from thallo.contracts.enums import UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Identity
from thallo.extensions import gbp as g
from thallo.extensions.gbp import persist

pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")
FIX = json.loads((Path(__file__).parent / "gbp_fixtures.json").read_text())


class Verifier:
    def is_live_human_session(self, actor):
        return actor.session_id == "s1"


def test_round_trip_isolation_and_append_only(database, urls):  # noqa: F811
    ck = database.clock
    tenant = make_tenant(database, "Gbp pg")
    ident = Identity(id=new_id(ck), created_at=ck.now(), email=f"{new_id(ck)}@example.invalid", display_name="Owner")
    with psycopg.connect(urls["admin"]) as conn:
        conn.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                     (ident.id, ident.created_at, ident.email, ident.display_name))
    with database.tenant(tenant.id) as tx:
        user = tx.insert(AppUser(id=new_id(ck), created_at=ck.now(), tenant_id=tenant.id, identity_id=ident.id,
                                 display_name="Owner", role=UserRole.APPROVER))
        items = g.parse_reviews(FIX["reviews_export"])
        ids = persist.write_items(tx, items)
        assert persist.write_items(tx, items) == ids  # a second import adds nothing
        item_id = ids["review:r-1"]
        d = g.person_draft(tenant=tenant, item_id=item_id, text="Thank you. Please call us.", clock=ck)
        persist.write_draft(tx, d)
        human = HumanActor(id=user.id, tenant_id=tenant.id, session_id="s1")
        dec = g.approve_reply(d, items[0], actor=human, approver=user, verifier=Verifier(),
                              shown_sha256=d.text_sha256, read_confirmed=True, clock=ck)
        persist.write_decision(tx, dec)
        p = g.propose(tenant_id=tenant.id, field="description", current=None, proposed="A family bike shop.",
                      reason="Clearer", proposed_by="agent", clock=ck)
        persist.write_proposal(tx, p)
    with database.tenant(tenant.id) as tx:
        drafts, decisions = persist.drafts(tx), persist.decisions(tx)
        assert [x.text_sha256 for x in drafts] == [d.text_sha256]
        assert g.standing(d.id, d.text_sha256, decisions) == "approved"
    other = make_tenant(database, "Other gbp")
    with database.tenant(other.id) as tx:
        assert persist.drafts(tx) == [] and persist.decisions(tx) == []
    for table in ("gbp_inbox_item", "gbp_reply_draft", "gbp_profile_proposal", "gbp_decision"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
            tx.execute(f"DELETE FROM {table}")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("UPDATE gbp_reply_draft SET text = 'changed'")
