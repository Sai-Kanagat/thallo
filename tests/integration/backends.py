"""Two backends for the wave 1 flow, behind one small facade.

`PgBackend` uses the real adapters (`thallo.store.adapters`) over `Database.tenant`, as
the per-run app role. `MemoryBackend` is an in-memory store with the same rules the database
enforces for this flow: rows are scoped to the session's tenant (row-level security), audit
seqs are unique per tenant and insert-only, claim versions are unique and append-only, and a
gate change is written all or nothing.

A session is one tenant transaction: `with backend.session(tenant_id) as s:` gives `s.gate`
(GateRepository), `s.audit` (AuditLog), `s.claims` (ClaimRepository and ClaimUsage), `s.write`
(a GateChange in one transaction) and typed `insert`, `get`, `list`, `update`.
"""
from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from thallo.contracts.clock import Clock
from thallo.contracts.enums import ApprovalSubject
from thallo.contracts.models import (
    Approval,
    AppUser,
    AuditEntry,
    Bundle,
    CheckResult,
    Claim,
    Consent,
    Identity,
    Media,
    Operator,
    Row,
    Tenant,
    TrackedLink,
    Variant,
    VariantClaim,
    VariantMedia,
)
from thallo.contracts.states import CLAIM, ClaimState
from thallo.gate.approvals import GateChange
from thallo.gate.canonical import normalise_url
from thallo.store.adapters import (
    BlobStore,
    ChangeNotApplied,
    ClaimWriteRefused,
    PgAuditStore,
    PgClaimRepository,
    PgGateRepository,
    TenantMismatch,
    write_gate_change,
)
from thallo.store.adapters.claims import CONTENT, LIFECYCLE
from thallo.trust.audit import AuditConflict, AuditLog

REDIRECT = "https://thallo.test/r/"


# ====================================================================== Postgres

class PgSession:
    def __init__(self, repo, blobs: BlobStore, clock: Clock) -> None:
        self.repo = repo
        self.tenant_id = repo.tenant_id
        self.gate = PgGateRepository(repo, blobs, redirect_prefixes=(REDIRECT,))
        self.audit = AuditLog(PgAuditStore(repo), clock)
        self.claims = PgClaimRepository(repo)

    def insert(self, row: Row) -> Row:
        return self.repo.insert(row)

    def get(self, model, row_id):
        return self.repo.get(model, row_id)

    def list(self, model, **where):
        return self.repo.list(model, **where)

    def update(self, model, row_id, **fields) -> bool:
        return self.repo.update(model, row_id, **fields)

    def write(self, change: GateChange) -> None:
        write_gate_change(self.repo, change)


class PgBackend:
    name = "postgres"

    def __init__(self, urls: dict[str, str], clock: Clock, blobs: BlobStore) -> None:
        from thallo.store import Database

        self.urls = urls
        self.clock = clock
        self.blobs = blobs
        self.db = Database(urls["app"], clock, admin_url=urls["admin"])

    def provision(self, operator: Operator, tenant: Tenant, identity: Identity) -> None:
        import psycopg

        self.db.create_operator(operator)
        self.db.create_tenant(tenant)
        with psycopg.connect(self.urls["admin"]) as conn:  # identities sit above tenants: owner only
            conn.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                         (identity.id, identity.created_at, identity.email, identity.display_name))

    @contextmanager
    def session(self, tenant_id: str) -> Iterator[PgSession]:
        with self.db.tenant(tenant_id) as repo:
            yield PgSession(repo, self.blobs, self.clock)

    def set_paused(self, paused: bool) -> None:
        import psycopg

        with psycopg.connect(self.urls["admin"]) as conn:
            conn.execute("UPDATE system_setting SET system_paused = %s", (paused,))


# ====================================================================== in memory

class _Tables:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Row]] = {}
        self.paused = False

    def copy(self) -> dict[str, dict[str, Row]]:
        return {t: dict(r) for t, r in self.rows.items()}


def _tenant_of(row: Row) -> str:
    return row.id if isinstance(row, Tenant) else row.tenant_id  # type: ignore[attr-defined]


class MemoryAuditStore:
    def __init__(self, s: MemorySession) -> None:
        self.s = s

    def _mine(self, tenant_id: str) -> list[AuditEntry]:
        self.s.own(tenant_id)
        return sorted(self.s.list(AuditEntry), key=lambda e: e.seq)

    def head(self, tenant_id):
        rows = self._mine(tenant_id)
        return rows[-1] if rows else None

    def insert(self, entry: AuditEntry) -> None:
        if any(e.seq == entry.seq for e in self._mine(entry.tenant_id)):
            raise AuditConflict(f"seq {entry.seq} already exists for this tenant")
        self.s.insert(entry)

    def entries(self, tenant_id):
        return tuple(self._mine(tenant_id))


class MemoryClaims:
    def __init__(self, s: MemorySession) -> None:
        self.s = s

    def _code(self, tenant_id, code) -> list[Claim]:
        self.s.own(tenant_id)
        return sorted(self.s.list(Claim, code=code), key=lambda c: c.version)

    def latest(self, tenant_id, code):
        rows = self._code(tenant_id, code)
        return rows[-1] if rows else None

    def history(self, tenant_id, code):
        return self._code(tenant_id, code)

    def latest_for_tenant(self, tenant_id):
        self.s.own(tenant_id)
        codes = sorted({c.code for c in self.s.list(Claim)})
        return [self.latest(tenant_id, k) for k in codes]

    def save(self, claim: Claim) -> None:
        versions = {c.version: c for c in self._code(claim.tenant_id, claim.code)}
        stored = versions.get(claim.version)
        if stored is None:
            before = versions.get(claim.version - 1)
            if claim.version > 1 and (before is None or claim.supersedes_id != before.id):
                raise ClaimWriteRefused(f"{claim.code} v{claim.version} must supersede v{claim.version - 1}")
            self.s.insert(claim)
            return
        if stored.id != claim.id:
            raise ClaimWriteRefused(f"{claim.code} v{claim.version} already exists")
        if any(getattr(stored, f) != getattr(claim, f) for f in CONTENT) and stored.state is not ClaimState.CANDIDATE:
            raise ClaimWriteRefused(f"{claim.code} v{claim.version}: stored versions are append-only")
        if claim.state is not stored.state:
            CLAIM.check(stored.state, claim.state)
        self.s.update(Claim, claim.id, **{f: getattr(claim, f) for f in (*CONTENT, *LIFECYCLE)})

    def variants_using_claim(self, tenant_id, claim_id):
        self.s.own(tenant_id)
        return sorted(vc.variant_id for vc in self.s.list(VariantClaim, claim_id=claim_id))

    def live_approvals_for_variants(self, tenant_id, variant_ids):
        self.s.own(tenant_id)
        bundles = {v.bundle_id for v in self.s.list(Variant) if v.id in set(variant_ids)}
        return sorted(a.id for a in self.s.list(Approval) if a.subject_kind is ApprovalSubject.BUNDLE
                      and a.subject_id in bundles and a.voided_at is None)


class MemoryGate:
    def __init__(self, s: MemorySession, blobs: BlobStore, tables: _Tables) -> None:
        self.s = s
        self.blobs = blobs
        self.tables = tables

    def _o(self, tenant_id):
        self.s.own(tenant_id)

    def system_paused(self):
        return self.tables.paused

    def get_tenant(self, tenant_id):
        self._o(tenant_id)
        return self.s.get(Tenant, tenant_id)

    def get_bundle(self, tenant_id, bundle_id):
        self._o(tenant_id)
        return self.s.get(Bundle, bundle_id)

    def get_variants(self, tenant_id, bundle_id):
        self._o(tenant_id)
        return self.s.list(Variant, bundle_id=bundle_id)

    def get_variant_media(self, tenant_id, variant_id):
        self._o(tenant_id)
        return [(vm, self.s.get(Media, vm.media_id)) for vm in self.s.list(VariantMedia, variant_id=variant_id)]

    def read_media_bytes(self, tenant_id, media):
        self._o(tenant_id)
        return self.blobs.read(tenant_id, media.storage_uri)

    def get_variant_claims(self, tenant_id, variant_id):
        self._o(tenant_id)
        rows = [self.s.get(Claim, vc.claim_id) for vc in self.s.list(VariantClaim, variant_id=variant_id)]
        return sorted(rows, key=lambda c: (c.code, c.id))

    def get_tracked_links(self, tenant_id, variant_id):
        self._o(tenant_id)
        return self.s.list(TrackedLink, variant_id=variant_id)

    def get_consent(self, tenant_id, consent_id):
        self._o(tenant_id)
        return self.s.get(Consent, consent_id)

    def get_approval(self, tenant_id, approval_id):
        self._o(tenant_id)
        return self.s.get(Approval, approval_id)

    def get_approvals(self, tenant_id, bundle_id):
        self._o(tenant_id)
        return self.s.list(Approval, subject_id=bundle_id)

    def get_user(self, tenant_id, user_id):
        self._o(tenant_id)
        return self.s.get(AppUser, user_id)

    def get_audit_entries(self, tenant_id, subject_kind, subject_id):
        return [e for e in MemoryAuditStore(self.s).entries(tenant_id)
                if e.subject_kind == subject_kind and e.subject_id == subject_id]

    def get_check_results(self, tenant_id, variant_id, content_sha256):
        self._o(tenant_id)
        from thallo.extensions.proof.gate import blocking_rows  # WP-28: withdrawn proof refuses at any hash
        rows = self.s.list(CheckResult, variant_id=variant_id, content_sha256=content_sha256)
        return [*rows, *blocking_rows(self.s.list, self.s.get, tenant_id=tenant_id, variant_id=variant_id,
                                      content_sha256=content_sha256, now=self.s.audit.clock.now())]

    def get_audit_chain(self, tenant_id, from_seq):
        return [e for e in MemoryAuditStore(self.s).entries(tenant_id) if e.seq >= from_seq]

    def audit_anchor(self, tenant_id):
        self._o(tenant_id)
        return None

    def non_link_allowlist(self, tenant_id) -> Collection[str]:
        self._o(tenant_id)
        return frozenset()

    def resolve_link(self, tenant_id, url):
        self._o(tenant_id)
        url = normalise_url(url)
        slug = url[len(REDIRECT):] if url.startswith(REDIRECT) else ""
        if slug and not set("/?#") & set(slug):
            rows = self.s.list(TrackedLink, slug=slug)
            return rows[0].destination if rows else None
        return None


class MemorySession:
    def __init__(self, tables: _Tables, tenant_id: str, blobs: BlobStore, clock: Clock) -> None:
        self.tables = tables
        self.tenant_id = tenant_id
        self.gate = MemoryGate(self, blobs, tables)
        self.audit = AuditLog(MemoryAuditStore(self), clock)
        self.claims = MemoryClaims(self)

    def own(self, tenant_id: str) -> None:
        if tenant_id != self.tenant_id:
            raise TenantMismatch("this repository is scoped to another tenant")

    def _table(self, model) -> dict[str, Row]:
        return self.tables.rows.setdefault(model.__table__, {})

    def insert(self, row: Row) -> Row:
        if _tenant_of(row) != self.tenant_id:
            raise PermissionError("row-level security: another tenant's row")
        table = self._table(type(row))
        if row.id in table:
            raise ValueError("duplicate id")
        table[row.id] = row
        return row

    def get(self, model, row_id):
        row = self._table(model).get(row_id)
        return row if row is not None and _tenant_of(row) == self.tenant_id else None

    def list(self, model, **where: Any) -> list:
        rows = [r for r in self._table(model).values() if _tenant_of(r) == self.tenant_id
                and all(getattr(r, k) == v for k, v in where.items())]
        return sorted(rows, key=lambda r: r.id)

    def update(self, model, row_id, **fields) -> bool:
        row = self.get(model, row_id)
        if row is None:
            return False
        self._table(model)[row_id] = row.model_copy(update=fields)
        return True

    def write(self, change: GateChange) -> None:
        self.own(change.tenant_id)
        saved = self.tables.copy()
        try:
            if change.new_approval is not None:
                self.insert(change.new_approval)
            for a in change.voided_approvals:
                if not self.update(Approval, a.id, voided_at=a.voided_at, void_reason=a.void_reason):
                    raise ChangeNotApplied(a.id)
            if not self.update(Bundle, change.bundle.id, state=change.bundle.state,
                               root_hash=change.bundle.root_hash):
                raise ChangeNotApplied(change.bundle.id)
            for v in change.variants:
                if not self.update(Variant, v.id, state=v.state, content_sha256=v.content_sha256):
                    raise ChangeNotApplied(v.id)
            for e in change.audit_entries:
                MemoryAuditStore(self).insert(e)
        except Exception:
            self.tables.rows = saved
            raise


class MemoryBackend:
    name = "memory"

    def __init__(self, clock: Clock, blobs: BlobStore) -> None:
        self.clock = clock
        self.blobs = blobs
        self.tables = _Tables()

    def provision(self, operator: Operator, tenant: Tenant, identity: Identity) -> None:
        self.tables.rows.setdefault("tenant", {})[tenant.id] = tenant

    @contextmanager
    def session(self, tenant_id: str) -> Iterator[MemorySession]:
        saved = self.tables.copy()
        try:
            yield MemorySession(self.tables, tenant_id, self.blobs, self.clock)
        except BaseException:
            self.tables.rows = saved  # the transaction rolls back
            raise

    def set_paused(self, paused: bool) -> None:
        self.tables.paused = paused


__all__: Sequence[str] = ["REDIRECT", "MemoryBackend", "PgBackend"]
