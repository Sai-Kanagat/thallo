"""Fixtures for the API tests: two tenants, each with a founder, a staff member and one bundle in
review (one variant, one image, one verified claim, one FLAG from the truth compiler, and the code
check rows bound to the variant's current hash). A third identity belongs to both tenants.

Every test that takes `world` runs twice: in memory, and on Postgres as the per-run app role
(the pattern of tests/wp01_store/conftest.py), with the owner-side directory reading identities.
No keys, no network: codes are captured by a test sender, the vault key is generated per test.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.backend import MemoryBackend, PgBackend
from api.context import CSRF_HEADER, Services
from api.directory import InMemoryDirectory, OwnerDirectory
from api.media_store import TenantMediaStore
from thallo.connections.registry import ProviderRegistry
from thallo.connections.vault import KeyRing, Vault
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import (
    Channel,
    CheckVerdict,
    ClaimKind,
    FunnelStage,
    Language,
    MediaKind,
    MediaProvenance,
    SourceKind,
    UserRole,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    AppUser,
    Asset,
    Brief,
    Bundle,
    Claim,
    GrowthModel,
    Idea,
    Identity,
    Media,
    Operator,
    Pillar,
    PlanSlot,
    Source,
    Tenant,
    Variant,
    VariantClaim,
    VariantMedia,
)
from thallo.contracts.states import BundleState, ClaimState, PlanSlotState, VariantState
from thallo.gate import content_hashes
from thallo.gate.signing import GateSigner
from thallo.media.lint import CHECKS as LINT_CHECKS
from thallo.media.lint import Finding as LintFinding
from thallo.media.lint import LintReport
from thallo.media.lint import to_check_results as lint_rows
from thallo.truth import build, to_check_results

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL")
TEXT = "Fino a {{C-0001}} con una carica, il top."
IMAGE = b"\x89PNG review deck image"
PASSING_LINT = LintReport(findings=tuple(LintFinding(check=n, verdict=CheckVerdict.PASS) for n in LINT_CHECKS))


class CapturingSender:
    """Keeps the last code per email in memory. Sends nothing anywhere."""

    def __init__(self) -> None:
        self.codes: dict[str, str] = {}
        self.calls = 0

    def send(self, email: str, code: str) -> None:
        self.calls += 1
        self.codes[email] = code


@pytest.fixture(scope="session")
def pg_urls():
    if not URL:
        pytest.skip("DATABASE_URL unset: the Postgres half runs under scripts/ci.sh")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    sys.path.insert(0, str(ROOT / "scripts"))
    from migrate import migrate

    name = f"cortex_wp09_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    params = conninfo_to_dict(URL)
    params["dbname"] = name
    admin_url = make_conninfo(**params)
    migrate(admin_url)
    password = secrets.token_hex(16)
    role = f"cortex_app_{secrets.token_hex(4)}"
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' IN ROLE cortex_app "
                     "NOSUPERUSER NOBYPASSRLS INHERIT")
    params.update(user=role, password=password)
    try:
        yield {"admin": admin_url, "app": make_conninfo(**params)}
    finally:
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"DROP ROLE IF EXISTS {role}")


@dataclass
class Shop:
    tenant: Tenant
    founder_email: str
    founder: AppUser
    staff_email: str
    staff: AppUser
    bundle_id: str
    variant_id: str
    media_id: str
    flag_ids: list[str]
    slot_ids: list[str] = field(default_factory=list)


@dataclass
class World:
    kind: str
    clock: FakeClock
    svc: Services
    sender: CapturingSender
    a: Shop
    b: Shop
    both_email: str
    media_root: Path
    urls: dict | None = None

    def client(self) -> TestClient:
        return TestClient(create_app(self.svc), base_url="https://testserver")

    def login(self, email: str, tenant_id: str | None = None) -> tuple[TestClient, str]:
        """Sign in through the real routes. Returns the client and its CSRF token."""
        c = self.client()
        csrf = c.get("/api/session").json()["csrf_token"]
        r = c.post("/api/auth/request-code", json={"email": email}, headers={CSRF_HEADER: csrf})
        assert r.status_code == 202, r.text
        r = c.post("/api/auth/verify", json={"email": email, "code": self.sender.codes[email]},
                   headers={CSRF_HEADER: csrf})
        assert r.status_code == 200, r.text
        csrf = r.json()["csrf_token"]
        if tenant_id is not None:
            r = c.post("/api/auth/tenant", json={"tenant_id": tenant_id}, headers={CSRF_HEADER: csrf})
            assert r.status_code == 200, r.text
            csrf = r.json()["csrf_token"]
        return c, csrf


def _ident(clock: FakeClock) -> str:
    clock.advance(seconds=1)
    return new_id(clock)


def _seed(kind: str, backend, provision, clock: FakeClock, name: str, media: TenantMediaStore,  # noqa: ANN001
          both: Identity) -> tuple[Shop, list]:
    now = clock.now()
    operator = Operator(id=_ident(clock), created_at=now, name=f"op {name}")
    tenant = Tenant(id=_ident(clock), created_at=now, operator_id=operator.id, name=name, currency="EUR",
                    slug=f"{name.lower()}-{secrets.token_hex(3)}", default_language=Language.IT, region_code="IT")
    founder_id = Identity(id=_ident(clock), created_at=now, email=f"founder-{secrets.token_hex(3)}@{name.lower()}.test",
                          display_name=f"Founder {name}")
    staff_id = Identity(id=_ident(clock), created_at=now, email=f"staff-{secrets.token_hex(3)}@{name.lower()}.test",
                        display_name=f"Staff {name}")
    provision(operator, tenant, [founder_id, staff_id])
    t = tenant.id
    with backend.tenant(t) as tx:
        founder = tx.insert(AppUser(id=_ident(clock), created_at=now, tenant_id=t, identity_id=founder_id.id,
                                    display_name=founder_id.display_name, role=UserRole.FOUNDER))
        staff = tx.insert(AppUser(id=_ident(clock), created_at=now, tenant_id=t, identity_id=staff_id.id,
                                  display_name=staff_id.display_name, role=UserRole.STAFF))
        shared = tx.insert(AppUser(id=_ident(clock), created_at=now, tenant_id=t, identity_id=both.id,
                                   display_name="Operator", role=UserRole.OPERATOR))
        source = tx.insert(Source(id=_ident(clock), created_at=now, tenant_id=t, kind=SourceKind.DOCUMENT,
                                  title="Spec sheet", captured_at=now))
        claim = tx.insert(Claim(id=_ident(clock), created_at=now, tenant_id=t, code="C-0001", kind=ClaimKind.NUMBER,
                                state=ClaimState.VERIFIED, value="120", unit="km", source_id=source.id,
                                evidence_sha256=hashlib.sha256(b"spec sheet page 2").hexdigest(),
                                verified_by=founder.id, verified_at=now, languages={"it": "120 km di autonomia"}))
        built = build(TEXT, tenant_id=t, language=Language.IT, claims={"C-0001": claim}, clock=clock,
                      default_language=Language.IT)
        idea = tx.insert(Idea(id=_ident(clock), created_at=now, tenant_id=t, stage=FunnelStage.CONSIDERATION,
                              title="Range", rationale="Riders ask about range"))
        brief = tx.insert(Brief(id=_ident(clock), created_at=now, tenant_id=t, idea_id=idea.id, audience="Commuters",
                                job_to_be_done="Get to work", message="Long range", call_to_action="Prenota",
                                language=Language.IT))
        asset = tx.insert(Asset(id=_ident(clock), created_at=now, tenant_id=t, brief_id=brief.id,
                                master_text=built.compiled.text, selection_reason="Range is the top question"))
        bundle = tx.insert(Bundle(id=_ident(clock), created_at=now, tenant_id=t, asset_id=asset.id,
                                  state=BundleState.IN_REVIEW, rule_version=1))
        variant = tx.insert(Variant(id=_ident(clock), created_at=now, tenant_id=t, bundle_id=bundle.id,
                                    channel=Channel.INSTAGRAM, language=Language.IT, state=VariantState.REVIEW,
                                    text=built.compiled.text, rendered_text=built.filled.text, task_type="offer_post"))
        for claim_id in built.compiled.claim_ids:
            tx.insert(VariantClaim(id=_ident(clock), created_at=now, tenant_id=t, variant_id=variant.id,
                                   claim_id=claim_id))
        uri = media.write(t, "hero.png", IMAGE)
        m = tx.insert(Media(id=_ident(clock), created_at=now, tenant_id=t, kind=MediaKind.IMAGE,
                            sha256=hashlib.sha256(IMAGE).hexdigest(), mime="image/png", size_bytes=len(IMAGE),
                            storage_uri=uri, provenance=MediaProvenance.TEMPLATE_RENDER, alt_text="Bici rossa"))
        tx.insert(VariantMedia(id=_ident(clock), created_at=now, tenant_id=t, variant_id=variant.id, media_id=m.id))
        sha = content_hashes(tx.gate, t, bundle.id)[variant.id]
        rows = to_check_results(built.compiled, variant_id=variant.id, content_sha256=sha, clock=clock)
        rows += lint_rows(PASSING_LINT, tenant_id=t, variant_id=variant.id, content_sha256=sha, clock=clock,
                          media_sha256=m.sha256)
        for row in rows:
            tx.insert(row)
        flag_ids = [r.id for r in rows if r.verdict is CheckVerdict.FLAG]
        assert len(flag_ids) == 1
        gm = tx.insert(GrowthModel(id=_ident(clock), created_at=now, tenant_id=t, version=1))
        pillar = tx.insert(Pillar(id=_ident(clock), created_at=now, tenant_id=t, growth_model_id=gm.id, name="Range",
                                  stage=FunnelStage.CONSIDERATION, target_share="0.6"))
        tx.insert(Pillar(id=_ident(clock), created_at=now, tenant_id=t, growth_model_id=gm.id, name="Rides",
                         stage=FunnelStage.AWARENESS, target_share="0.4"))
        week = date(2026, 1, 5)
        slots = []
        for i, state in enumerate((PlanSlotState.OPEN, PlanSlotState.OPEN, PlanSlotState.MISSED)):
            s = tx.insert(PlanSlot(id=_ident(clock), created_at=now, tenant_id=t, week_start=week,
                                   channel=Channel.INSTAGRAM, pillar_id=pillar.id, stage=FunnelStage.CONSIDERATION,
                                   starts_at=FAKE_EPOCH + timedelta(days=i), state=state))
            slots.append(s.id)
    shop = Shop(tenant=tenant, founder_email=founder_id.email, founder=founder, staff_email=staff_id.email,
                staff=staff, bundle_id=bundle.id, variant_id=variant.id, media_id=m.id, flag_ids=flag_ids,
                slot_ids=slots)
    return shop, [founder, staff, shared]


@pytest.fixture(params=["memory", "postgres"])
def world(request, tmp_path) -> World:
    clock = FakeClock(FAKE_EPOCH)
    root = (tmp_path / "brand").resolve()
    root.mkdir()
    media = TenantMediaStore(root)
    directory: object
    urls = None
    if request.param == "memory":
        backend = MemoryBackend(clock, media)
        mem = InMemoryDirectory()
        directory = mem

        def provision(op, tenant, identities):  # noqa: ANN001
            backend.add_tenant(tenant)
            mem.tenants[tenant.id] = tenant
            for i in identities:
                mem.identities[i.id] = i
    else:
        import psycopg

        from thallo.store import Database

        urls = request.getfixturevalue("pg_urls")
        backend = PgBackend(urls["app"], clock, media)
        admin_db = Database(urls["app"], clock, admin_url=urls["admin"])
        directory = OwnerDirectory(urls["admin"])

        def provision(op, tenant, identities):  # noqa: ANN001
            admin_db.create_operator(op)
            admin_db.create_tenant(tenant)
            with psycopg.connect(urls["admin"]) as conn:  # identities sit above tenants: owner only
                for i in identities:
                    conn.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                                 (i.id, i.created_at, i.email, i.display_name))
    now = clock.now()
    both = Identity(id=_ident(clock), created_at=now, email=f"both-{secrets.token_hex(3)}@ops.test",
                    display_name="Operator Both")
    if request.param == "memory":
        directory.identities[both.id] = both  # type: ignore[attr-defined]
    else:
        import psycopg

        with psycopg.connect(urls["admin"]) as conn:
            conn.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                         (both.id, both.created_at, both.email, both.display_name))
    a, users_a = _seed(request.param, backend, provision, clock, "Alpha", media, both)
    b, users_b = _seed(request.param, backend, provision, clock, "Beta", media, both)
    if request.param == "memory":
        directory.users.extend(users_a + users_b)  # type: ignore[attr-defined]
    _conns, secret_store, _health = backend.connection_stores()
    vault = Vault(KeyRing(os.urandom(32)), secret_store, clock)
    sender = CapturingSender()
    svc = Services(clock=clock, backend=backend, directory=directory, sender=sender, media=media,  # type: ignore[arg-type]
                   signer=GateSigner(secrets.token_bytes(32)), providers=ProviderRegistry.with_discovered(),
                   vault=vault, mock_tenants=(a.tenant.id, b.tenant.id))
    return World(kind=request.param, clock=clock, svc=svc, sender=sender, a=a, b=b, both_email=both.email,
                 media_root=root, urls=urls)
