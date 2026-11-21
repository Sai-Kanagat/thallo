"""WP-29 on Postgres: subscribe and unsubscribe as the app role (consent withdrawal), one delivery per
subscriber per issue, append-only issue rows, tenant isolation."""
from __future__ import annotations

from datetime import date

import psycopg
import pytest

from tests.wp01_store.conftest import URL, admin, database, make_tenant, urls  # noqa: F401
from thallo.contracts.actors import HumanActor
from thallo.contracts.enums import Channel, FunnelStage, Language, UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Asset, Brief, Bundle, Consent, Idea, Identity, Person, Variant
from thallo.extensions.newsletter import NewsletterIssue, NewsletterSubscriber, subscribe, unsubscribe

pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")


class Verifier:
    def is_live_human_session(self, actor):
        return actor.session_id == "live"


def seed(database, admin, tenant):  # noqa: F811
    clock = database.clock
    ident = Identity(id=new_id(clock), created_at=clock.now(), email=f"{new_id(clock)}@example.invalid",
                     display_name="Founder")
    admin.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                  (ident.id, ident.created_at, ident.email, ident.display_name))
    with database.tenant(tenant.id) as tx:
        now = clock.now()
        user = tx.insert(AppUser(id=new_id(clock), created_at=now, tenant_id=tenant.id, identity_id=ident.id,
                                 display_name="Founder", role=UserRole.FOUNDER))
        person = tx.insert(Person(id=new_id(clock), created_at=now, tenant_id=tenant.id, email="reader@example.com"))
        idea = tx.insert(Idea(id=new_id(clock), created_at=now, tenant_id=tenant.id, stage=FunnelStage.AWARENESS,
                              title="t", rationale="r"))
        brief = tx.insert(Brief(id=new_id(clock), created_at=now, tenant_id=tenant.id, idea_id=idea.id, audience="a",
                                job_to_be_done="j", message="m", call_to_action="c", language=Language.EN))
        asset = tx.insert(Asset(id=new_id(clock), created_at=now, tenant_id=tenant.id, brief_id=brief.id,
                                master_text="x"))
        bundle = tx.insert(Bundle(id=new_id(clock), created_at=now, tenant_id=tenant.id, asset_id=asset.id,
                                  rule_version=1))
        v = tx.insert(Variant(id=new_id(clock), created_at=now, tenant_id=tenant.id, bundle_id=bundle.id,
                              channel=Channel.NEWSLETTER, language=Language.EN, text="Issue",
                              task_type="newsletter_issue"))
        issue = tx.insert(NewsletterIssue(id=new_id(clock), created_at=now, tenant_id=tenant.id,
                                          period_start=date(2026, 11, 1), period_end=date(2026, 11, 14),
                                          variant_id=v.id, piece_brief_ids=[brief.id]))
    return user, person, issue


def test_subscribe_unsubscribe_and_guards(database, admin):  # noqa: F811
    tenant = make_tenant(database, "Letters")
    user, person, issue = seed(database, admin, tenant)
    clock = database.clock
    actor = HumanActor(id=user.id, tenant_id=tenant.id, session_id="live")
    with database.tenant(tenant.id) as tx:
        sub = subscribe(tx, tenant_id=tenant.id, clock=clock, person_id=person.id, email="Reader@Example.com",
                        evidence="ticked the form box", actor=actor, verifier=Verifier())
        assert sub.recipient == "reader@example.com"
    with database.tenant(tenant.id) as tx:
        assert unsubscribe(tx, token=sub.token, clock=clock)
        assert tx.get(Consent, sub.consent_id).withdrawn_at is not None
        assert tx.get(NewsletterSubscriber, sub.id).unsubscribed_at is not None
    with pytest.raises(psycopg.errors.UniqueViolation), database.tenant(tenant.id) as tx:
        tx.insert(issue.model_copy(update={"id": new_id(clock)}))  # one issue per period
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("UPDATE newsletter_issue SET period_end = period_end + 1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(tenant.id) as tx:
        tx.execute("DELETE FROM newsletter_subscriber")
    other = make_tenant(database, "Other letters")
    with database.tenant(other.id) as tx:
        assert tx.list_all(NewsletterSubscriber) == [] and tx.list_all(NewsletterIssue) == []
        assert not unsubscribe(tx, token=sub.token, clock=clock)
