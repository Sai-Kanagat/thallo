"""WP-18 on Postgres as a NOBYPASSRLS app role: the same scenario, the conditional sign, the
signed audit entry, tenant isolation and the table's checks. Skipped without DATABASE_URL."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

from tests.wp18_measure.scenarios import strong_pillar
from tests.wp18_measure.test_measure import check_pillar_change
from tests.wp18_measure.world import Seeder
from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Language, UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Operator, Tenant
from thallo.gate.signing import GateSigner
from thallo.measure import decide
from thallo.measure.decide import DecisionRefused, propose_month
from thallo.measure.records import DecisionProposal, DecisionState
from thallo.store.adapters import PgAuditStore
from thallo.trust.audit import AuditLog

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="DATABASE_URL unset: run scripts/ci.sh for database tests")
SIGNER = GateSigner(bytes(range(32)))


@pytest.fixture(autouse=True)
def no_network():  # this file talks to the local Postgres on purpose
    yield


@pytest.fixture(scope="module")
def urls():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_wp18_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(URL)
    params["dbname"] = name
    admin_url = make_conninfo(**params)
    migrate(admin_url)
    pw, role = secrets.token_hex(16), f"cortex_app_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' IN ROLE cortex_app NOSUPERUSER NOBYPASSRLS INHERIT")
    try:
        yield {"admin": admin_url, "app": make_conninfo(**{**params, "user": role, "password": pw})}
    finally:
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"DROP ROLE IF EXISTS {role}")


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def database(urls, clock):
    from thallo.store import Database

    return Database(urls["app"], clock, admin_url=urls["admin"])


class Verifier:
    def is_live_human_session(self, actor):
        return actor.session_id == "live"


def _tenant(database, urls, name):
    import psycopg

    clock = database.clock
    op = database.create_operator(Operator(id=new_id(clock), created_at=clock.now(), name=f"op-{name}"))
    t = database.create_tenant(Tenant(id=new_id(clock), created_at=clock.now(), name=name, operator_id=op.id,
                                      currency="EUR", slug=f"{name.lower()}-{secrets.token_hex(3)}",
                                      default_language=Language.EN, region_code="IT"))
    ident = new_id(clock)
    with psycopg.connect(urls["admin"]) as conn:
        conn.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                     (ident, clock.now(), f"f-{secrets.token_hex(3)}@example.test", "Founder"))
    with database.tenant(t.id) as repo:
        user = repo.insert(AppUser(id=new_id(clock), created_at=clock.now(), tenant_id=t.id, identity_id=ident,
                                   display_name="Founder", role=UserRole.FOUNDER))
    return t, user


def test_table_has_rls(urls):
    import psycopg

    from thallo.store.rls import audit_rls

    with psycopg.connect(urls["admin"]) as conn:
        assert audit_rls(conn) == []
        assert conn.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'decision_proposal'").fetchone()[0]


def test_scenario_sign_once_and_isolation(database, urls, clock):
    a, founder = _tenant(database, urls, "Alpha")
    b, _ = _tenant(database, urls, "Beta")
    clock.advance(days=120)
    with database.tenant(a.id) as repo:
        strong_pillar(Seeder(repo, clock, founder.id))
        p = check_pillar_change(propose_month(repo, clock))
        assert [r.id for r in propose_month(repo, clock)] == [p.id]
    actor = HumanActor(id=founder.id, tenant_id=a.id, session_id="live")
    with database.tenant(a.id) as repo:
        audit = AuditLog(PgAuditStore(repo), clock)
        stale = decide._open(repo, p.id, p.content_sha256)
        decide.sign(repo, clock, p.id, p.content_sha256, actor=actor, verifier=Verifier(), approver=founder,
                    audit=audit, signer=SIGNER)
        with pytest.raises(DecisionRefused, match="already acted"):
            decide._transition(repo, stale, state=DecisionState.DECLINED, declined_by=founder.id,
                               declined_at=clock.now())
        stored = repo.get(DecisionProposal, p.id)
        entries = [e for e in audit.store.entries(a.id) if e.subject_id == p.id]
    assert stored.state is DecisionState.SIGNED and stored.signed_by == founder.id
    assert [e.action for e in entries] == [decide.ACTION_SIGNED]
    with database.tenant(b.id) as repo:
        assert repo.list_all(DecisionProposal) == []
        assert repo.get(DecisionProposal, p.id) is None


def test_signed_state_needs_who_and_when(database, urls, clock):
    import psycopg

    a, founder = _tenant(database, urls, "Gamma")
    clock.advance(days=120)
    with database.tenant(a.id) as repo:
        strong_pillar(Seeder(repo, clock, founder.id))
        [p] = propose_month(repo, clock)
    with pytest.raises(psycopg.errors.CheckViolation), database.tenant(a.id) as repo:
        repo.execute("UPDATE decision_proposal SET state = 'signed' WHERE id = %s", (p.id,))
