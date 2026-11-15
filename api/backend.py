"""Where the API reads and writes: one tenant transaction per request.

`with backend.tenant(tenant_id) as tx:` gives `tx.gate` (the gate's `GateRepository`), `tx.audit`
(an `AuditLog` on the same transaction), typed `get`, `list_all`, `insert`, `update`, and
`tx.write(change)`, which writes a `GateChange` in one transaction (gate/AUDIT.md). Anything the
route does in the same block (an edit, an `edit` row) commits or rolls back with it.

`PgBackend` runs every statement through `Database(...).tenant(tenant_id)` as the app role, so
row-level security applies, using the store's adapters (`PgGateRepository`, `write_gate_change`).
`MemoryBackend` keeps the same rules in memory for tests and the demo: rows scoped to the tenant,
insert-only audit with unique seqs per tenant, a change all or nothing.

Connections use the connection stores of WP-02 (`thallo.connections`), which open their own tenant
transactions; `connection_stores()` returns the pair for each backend.
"""
from __future__ import annotations

from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from thallo.connections.stores import (
    ConnectionStore,
    HealthState,
    InMemoryConnectionStore,
    InMemoryHealthState,
    InMemorySecretStore,
    SecretStore,
)
from thallo.contracts.clock import Clock
from thallo.contracts.models import (
    Approval,
    AppUser,
    AuditEntry,
    Bundle,
    CheckResult,
    Claim,
    Consent,
    Media,
    Row,
    Tenant,
    TrackedLink,
    Variant,
    VariantClaim,
    VariantMedia,
)
from thallo.gate.approvals import GateChange
from thallo.gate.canonical import normalise_url
from thallo.gate.repository import GateRepository
from thallo.store.adapters import (
    BlobStore,
    ChangeNotApplied,
    PgAuditStore,
    PgGateRepository,
    TenantMismatch,
    write_gate_change,
)
from thallo.trust.audit import AuditConflict, AuditLog


class TenantTx(Protocol):
    tenant_id: str
    gate: GateRepository
    audit: AuditLog

    def get(self, model: type, row_id: str) -> Any: ...
    def list_all(self, model: type, **where: Any) -> list: ...
    def insert(self, row: Row) -> Row: ...
    def update(self, model: type, row_id: str, **fields: Any) -> bool: ...
    def write(self, change: GateChange) -> None: ...


class Backend(Protocol):
    clock: Clock
    blobs: BlobStore

    def tenant(self, tenant_id: str) -> Any: ...  # a context manager yielding a TenantTx
    def connection_stores(self) -> tuple[ConnectionStore, SecretStore, HealthState]: ...


# ====================================================================== Postgres

class PgTx:
    def __init__(self, repo, blobs: BlobStore, clock: Clock, redirect_prefixes: Sequence[str],  # noqa: ANN001
                 vouched_links: Collection[str]) -> None:
        self.repo = repo
        self.tenant_id = repo.tenant_id
        self.gate = PgGateRepository(repo, blobs, redirect_prefixes=redirect_prefixes, vouched_links=vouched_links)
        self.audit = AuditLog(PgAuditStore(repo), clock)

    def get(self, model, row_id):  # noqa: ANN001, ANN201
        return self.repo.get(model, row_id)

    def list_all(self, model, **where):  # noqa: ANN001, ANN201
        return self.repo.list_all(model, **where)

    def insert(self, row: Row) -> Row:
        return self.repo.insert(row)

    def update(self, model, row_id, **fields) -> bool:  # noqa: ANN001
        return self.repo.update(model, row_id, **fields)

    def write(self, change: GateChange) -> None:
        write_gate_change(self.repo, change)


class PgBackend:
    def __init__(self, app_url: str, clock: Clock, blobs: BlobStore, *, redirect_prefixes: Sequence[str] = (),
                 vouched_links: Collection[str] = ()) -> None:
        from thallo.store import Database

        self.db = Database(app_url, clock)
        self.clock = clock
        self.blobs = blobs
        self.redirect_prefixes = tuple(redirect_prefixes)
        self.vouched_links = tuple(vouched_links)

    @contextmanager
    def tenant(self, tenant_id: str) -> Iterator[PgTx]:
        with self.db.tenant(tenant_id) as repo:
            yield PgTx(repo, self.blobs, self.clock, self.redirect_prefixes, self.vouched_links)

    def connection_stores(self) -> tuple[ConnectionStore, SecretStore, HealthState]:
        from thallo.connections.pg import PostgresConnectionStore, PostgresHealthState, PostgresSecretStore

        return PostgresConnectionStore(self.db), PostgresSecretStore(self.db), PostgresHealthState(self.db)


# ====================================================================== in memory

def _tenant_of(row: Row) -> str:
    return row.id if isinstance(row, Tenant) else row.tenant_id  # type: ignore[attr-defined]


class _Tables:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Row]] = {}
        self.paused = False

    def copy(self) -> dict[str, dict[str, Row]]:
        return {t: dict(r) for t, r in self.rows.items()}


class MemoryAuditStore:
    def __init__(self, tx: MemoryTx) -> None:
        self.tx = tx

    def _mine(self, tenant_id: str) -> list[AuditEntry]:
        self.tx.own(tenant_id)
        return sorted(self.tx.list_all(AuditEntry), key=lambda e: e.seq)

    def head(self, tenant_id: str) -> AuditEntry | None:
        rows = self._mine(tenant_id)
        return rows[-1] if rows else None

    def insert(self, entry: AuditEntry) -> None:
        if any(e.seq == entry.seq for e in self._mine(entry.tenant_id)):
            raise AuditConflict(f"seq {entry.seq} already exists for this tenant")
        self.tx.insert(entry)

    def entries(self, tenant_id: str) -> tuple[AuditEntry, ...]:
        return tuple(self._mine(tenant_id))


class MemoryGate:
    """The gate's repository over the in-memory tables; the same reads as `PgGateRepository`."""

    def __init__(self, tx: MemoryTx, blobs: BlobStore, redirect_prefixes: Sequence[str],
                 vouched_links: Collection[str]) -> None:
        self.tx = tx
        self.blobs = blobs
        self.redirect_prefixes = tuple(normalise_url(p) for p in redirect_prefixes)
        self.vouched_links = frozenset(normalise_url(u) for u in vouched_links)

    def _o(self, tenant_id: str) -> None:
        self.tx.own(tenant_id)

    def system_paused(self) -> bool:
        return self.tx.tables.paused

    def get_tenant(self, tenant_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.get(Tenant, tenant_id)

    def get_bundle(self, tenant_id, bundle_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.get(Bundle, bundle_id)

    def get_variants(self, tenant_id, bundle_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.list_all(Variant, bundle_id=bundle_id)

    def get_variant_media(self, tenant_id, variant_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        out = []
        for link in self.tx.list_all(VariantMedia, variant_id=variant_id):
            media = self.tx.get(Media, link.media_id)
            if media is None:
                raise LookupError(f"media {link.media_id} is missing")
            out.append((link, media))
        return out

    def read_media_bytes(self, tenant_id, media):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        if media.tenant_id != tenant_id:
            raise PermissionError("media belongs to another tenant")
        return self.blobs.read(tenant_id, media.storage_uri)

    def get_variant_claims(self, tenant_id, variant_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        rows = [self.tx.get(Claim, vc.claim_id) for vc in self.tx.list_all(VariantClaim, variant_id=variant_id)]
        return sorted((r for r in rows if r is not None), key=lambda c: (c.code, c.id))

    def get_tracked_links(self, tenant_id, variant_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.list_all(TrackedLink, variant_id=variant_id)

    def get_consent(self, tenant_id, consent_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.get(Consent, consent_id)

    def get_approval(self, tenant_id, approval_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.get(Approval, approval_id)

    def get_approvals(self, tenant_id, bundle_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.list_all(Approval, subject_id=bundle_id)

    def get_user(self, tenant_id, user_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.get(AppUser, user_id)

    def get_audit_entries(self, tenant_id, subject_kind, subject_id):  # noqa: ANN001, ANN201
        return [e for e in MemoryAuditStore(self.tx).entries(tenant_id)
                if e.subject_kind == subject_kind and e.subject_id == subject_id]

    def get_check_results(self, tenant_id, variant_id, content_sha256):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        return self.tx.list_all(CheckResult, variant_id=variant_id, content_sha256=content_sha256)

    def get_audit_chain(self, tenant_id, from_seq):  # noqa: ANN001, ANN201
        return [e for e in MemoryAuditStore(self.tx).entries(tenant_id) if e.seq >= from_seq]

    def audit_anchor(self, tenant_id):  # noqa: ANN001, ANN201
        self._o(tenant_id)

    def non_link_allowlist(self, tenant_id) -> Collection[str]:  # noqa: ANN001
        self._o(tenant_id)
        return frozenset()

    def resolve_link(self, tenant_id, url):  # noqa: ANN001, ANN201
        self._o(tenant_id)
        url = normalise_url(url)
        for prefix in self.redirect_prefixes:
            slug = url[len(prefix):] if url.startswith(prefix) else ""
            if slug and not set("/?#") & set(slug):
                rows = self.tx.list_all(TrackedLink, slug=slug)
                return rows[0].destination if rows else None
        return url if url in self.vouched_links else None


class MemoryTx:
    def __init__(self, tables: _Tables, tenant_id: str, blobs: BlobStore, clock: Clock,
                 redirect_prefixes: Sequence[str], vouched_links: Collection[str]) -> None:
        self.tables = tables
        self.tenant_id = tenant_id
        self.gate = MemoryGate(self, blobs, redirect_prefixes, vouched_links)
        self.audit = AuditLog(MemoryAuditStore(self), clock)

    def own(self, tenant_id: str) -> None:
        if tenant_id != self.tenant_id:
            raise TenantMismatch("this repository is scoped to another tenant")

    def _table(self, model) -> dict[str, Row]:  # noqa: ANN001
        return self.tables.rows.setdefault(model.__table__, {})

    def insert(self, row: Row) -> Row:
        if _tenant_of(row) != self.tenant_id:
            raise PermissionError("row-level security: another tenant's row")
        table = self._table(type(row))
        if row.id in table:
            raise ValueError("duplicate id")
        table[row.id] = row
        return row

    def get(self, model, row_id):  # noqa: ANN001, ANN201
        row = self._table(model).get(row_id)
        return row if row is not None and _tenant_of(row) == self.tenant_id else None

    def list_all(self, model, **where: Any) -> list:  # noqa: ANN001
        rows = [r for r in self._table(model).values() if _tenant_of(r) == self.tenant_id
                and all(getattr(r, k) == v for k, v in where.items())]
        return sorted(rows, key=lambda r: r.id)

    def update(self, model, row_id, **fields) -> bool:  # noqa: ANN001
        for protected in ("id", "tenant_id", "created_at"):
            if protected in fields:
                raise ValueError(f"{protected} cannot be updated")
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
    def __init__(self, clock: Clock, blobs: BlobStore, *, redirect_prefixes: Sequence[str] = (),
                 vouched_links: Collection[str] = ()) -> None:
        self.clock = clock
        self.blobs = blobs
        self.tables = _Tables()
        self.redirect_prefixes = tuple(redirect_prefixes)
        self.vouched_links = tuple(vouched_links)
        self._stores = (InMemoryConnectionStore(), InMemorySecretStore(), InMemoryHealthState())

    def add_tenant(self, tenant: Tenant) -> None:
        self.tables.rows.setdefault("tenant", {})[tenant.id] = tenant

    @contextmanager
    def tenant(self, tenant_id: str) -> Iterator[MemoryTx]:
        saved = self.tables.copy()
        try:
            yield MemoryTx(self.tables, tenant_id, self.blobs, self.clock, self.redirect_prefixes,
                           self.vouched_links)
        except BaseException:
            self.tables.rows = saved  # the transaction rolls back
            raise

    def connection_stores(self) -> tuple[ConnectionStore, SecretStore, HealthState]:
        return self._stores


__all__ = ["Backend", "MemoryBackend", "MemoryTx", "PgBackend", "PgTx", "TenantTx"]
