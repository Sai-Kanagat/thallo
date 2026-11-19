"""Migration 0024 on Postgres: tenant isolation, the narrow redirector role, fixed links, and a test
lead through each entry point. Skipped without DATABASE_URL (scripts/ci.sh sets it)."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import CampaignRole, Language, TouchKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import Ad, Campaign, Lead, Operator, Person, Tenant, Touch
from thallo.tracking.capture import capture_call, capture_form, capture_walk_in, capture_whatsapp, form_fields
from thallo.tracking.links import mint_code, mint_link, mint_qr, mint_whatsapp_link
from thallo.tracking.models import CodeKind, TrackingCode
from thallo.tracking.redirector import Redirector
from thallo.tracking.wiring import PgLinkDirectory, backend_recorder

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL")
PREFIX = "https://go.example/r/"
pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")


@pytest.fixture(scope="module")
def urls():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_wp12_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(URL)
    params["dbname"] = name
    admin_url = make_conninfo(**params)
    migrate(admin_url)
    app_pw, red_pw = secrets.token_hex(16), secrets.token_hex(16)
    app_role, red_role = f"cortex_app_{secrets.token_hex(4)}", f"cortex_red_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {app_role} LOGIN PASSWORD '{app_pw}' IN ROLE cortex_app "
                     "NOSUPERUSER NOBYPASSRLS INHERIT")
        conn.execute(f"CREATE ROLE {red_role} LOGIN PASSWORD '{red_pw}' IN ROLE cortex_redirector "
                     "NOSUPERUSER NOBYPASSRLS INHERIT")
    try:
        yield {"admin": admin_url, "app": make_conninfo(**{**params, "user": app_role, "password": app_pw}),
               "redirector": make_conninfo(**{**params, "user": red_role, "password": red_pw})}
    finally:
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"DROP ROLE IF EXISTS {app_role}")
            admin.execute(f"DROP ROLE IF EXISTS {red_role}")


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def database(urls, clock):
    from thallo.store import Database

    return Database(urls["app"], clock, admin_url=urls["admin"])


def _tenant(database, name):
    clock = database.clock
    clock.advance(seconds=1)
    op = database.create_operator(Operator(id=new_id(clock), created_at=clock.now(), name=f"op-{name}"))
    return database.create_tenant(Tenant(id=new_id(clock), created_at=clock.now(), name=name, operator_id=op.id,
                                         currency="EUR", slug=f"{name.lower()}-{secrets.token_hex(3)}",
                                         default_language=Language.EN, region_code="IT"))


def _ad(repo, clock):
    camp = Campaign(id=new_id(clock), created_at=clock.now(), tenant_id=repo.tenant_id, platform="meta", name="c",
                    role=CampaignRole.LOCAL_SOCIAL)
    repo.insert(camp)
    ad = Ad(id=new_id(clock), created_at=clock.now(), tenant_id=repo.tenant_id, campaign_id=camp.id, platform="meta")
    repo.insert(ad)
    return ad.id


def test_every_table_is_protected_and_the_redirector_policy_is_select_only(urls):
    import psycopg

    from thallo.store.rls import audit_rls

    with psycopg.connect(urls["admin"]) as conn:
        assert audit_rls(conn) == []
        rows = conn.execute("SELECT polcmd, polroles::regrole[]::text[] FROM pg_policy "
                            "WHERE polrelid = 'tracked_link'::regclass AND polname = 'redirector_lookup'").fetchall()
        assert rows == [("r", ["cortex_redirector"])]
        cols = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                           "WHERE table_name = 'touch'").fetchall()}
        assert not {c for c in cols if "ip" in c.split("_") or "addr" in c or "agent" in c}


def test_tracking_code_is_tenant_isolated(database, clock):
    a, b = _tenant(database, "Alpha"), _tenant(database, "Beta")
    with database.tenant(a.id) as repo:
        code = mint_code(repo, clock, kind=CodeKind.OFFER, label="flyer")
    with database.tenant(b.id) as repo:
        assert repo.list_all(TrackingCode) == []
        assert capture_walk_in(repo, clock, code.code).code is None


def test_redirector_role_reads_four_columns_and_nothing_else(database, clock, urls):
    import psycopg

    a, b = _tenant(database, "Alpha"), _tenant(database, "Beta")
    with database.tenant(a.id) as repo:
        la = mint_link(repo, clock, prefix=PREFIX, destination="https://a.example/")
    with database.tenant(b.id) as repo:
        lb = mint_link(repo, clock, prefix=PREFIX, destination="https://b.example/")
    d = PgLinkDirectory(urls["redirector"])
    assert d.lookup(la.link.slug).tenant_id == a.id and d.lookup(lb.link.slug).destination == lb.link.destination
    assert d.lookup("zzzzzzzzzz") is None
    for stmt in ("SELECT variant_id FROM tracked_link", "SELECT whatsapp_code FROM tracked_link",
                 "SELECT * FROM touch", "SELECT * FROM person", "SELECT * FROM tracking_code",
                 "UPDATE tracked_link SET destination = 'https://evil.example/'",
                 "DELETE FROM tracked_link",
                 f"INSERT INTO touch (id, created_at, tenant_id, kind, occurred_at) VALUES "
                 f"('{new_id(clock)}', now(), '{a.id}', 'link', now())"):
        with psycopg.connect(urls["redirector"]) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(stmt)


def test_a_tracked_link_is_fixed_once_written(database, clock):
    import psycopg

    a = _tenant(database, "Alpha")
    with database.tenant(a.id) as repo:
        link = mint_link(repo, clock, prefix=PREFIX, destination="https://a.example/")
    for field, value in (("destination", "https://evil.example/"), ("slug", "aaaaaaaaaa"), ("whatsapp_code", "ACDEFH")):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.tenant(a.id) as repo:
            repo.execute(f"UPDATE tracked_link SET {field} = %s WHERE id = %s", (value, link.link.id))


def test_click_to_touch_end_to_end_on_postgres(database, clock, urls, tmp_path):
    from api.backend import PgBackend
    from api.media_store import TenantMediaStore

    a = _tenant(database, "Alpha")
    backend = PgBackend(urls["app"], clock, TenantMediaStore(tmp_path), redirect_prefixes=(PREFIX,))
    with database.tenant(a.id) as repo:
        ad_id = _ad(repo, clock)
        link = mint_link(repo, clock, prefix=PREFIX, destination="https://a.example/spring", ad_id=ad_id)
    r = Redirector(PgLinkDirectory(urls["redirector"]), backend_recorder(backend, clock), clock)
    out = r.handle(link.link.slug, client="c1", network="n1")
    with backend.tenant(a.id) as tx:
        assert out.status == 302 and out.counted and out.location == tx.gate.resolve_link(a.id, link.public_url)
        touches = tx.list_all(Touch)
    assert [(t.kind, t.ad_id, t.tracked_link_id, t.person_id) for t in touches] == [
        (TouchKind.LINK, ad_id, link.link.id, None)]


def test_a_lead_through_each_entry_point_lands_with_the_right_source(database, clock):
    a = _tenant(database, "Alpha")
    with database.tenant(a.id) as repo:
        ad_id = _ad(repo, clock)
        link = mint_link(repo, clock, prefix=PREFIX, destination="https://a.example/", ad_id=ad_id)
        wa = mint_whatsapp_link(repo, clock, prefix=PREFIX, business_number="393331234567", ad_id=ad_id)
        qr = mint_qr(repo, clock, prefix=PREFIX, destination="https://a.example/qr", ad_id=ad_id)
        walk = mint_code(repo, clock, kind=CodeKind.WALK_IN, ad_id=ad_id)
        call = mint_code(repo, clock, kind=CodeKind.CALL, ad_id=ad_id)
        entries = {
            "whatsapp": lambda pid: capture_whatsapp(repo, clock, f"hi ref {wa.link.whatsapp_code}", person_id=pid),
            "form": lambda pid: capture_form(repo, clock, form_fields(link.link), person_id=pid)[0],
            "qr_form": lambda pid: capture_form(repo, clock, form_fields(qr.link), person_id=pid)[0],
            "call": lambda pid: capture_call(repo, clock, call.code, person_id=pid),
            "walk_in": lambda pid: capture_walk_in(repo, clock, walk.code, person_id=pid),
        }
        for name, enter in entries.items():
            person = Person(id=new_id(clock), created_at=clock.now(), tenant_id=a.id, display_name=name)
            repo.insert(person)
            touch = enter(person.id)
            repo.insert(Lead(id=new_id(clock), created_at=clock.now(), tenant_id=a.id, person_id=person.id,
                             first_touch_id=touch.id, last_touch_id=touch.id))
        leads = repo.list_all(Lead)
        touches = {t.id: t for t in repo.list_all(Touch)}
    assert len(leads) == 5
    for lead in leads:
        assert touches[lead.first_touch_id].ad_id == ad_id
    kinds = sorted(touches[lead.first_touch_id].kind.value for lead in leads)
    assert kinds == ["call", "form", "form", "walk_in_code", "whatsapp_code"]
