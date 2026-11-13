"""Postgres implementations of what the gate, the audit log and the truth library read and write.

Every adapter wraps one `TenantRepo` (one tenant transaction from `Database.tenant`), so row-level
security scopes every statement, and a call naming another tenant raises `TenantMismatch`.

- `PgGateRepository`: the gate's `GateRepository`; media bytes come through a `BlobStore`
- `PgAuditStore`: the audit log's insert-only `AuditStore`
- `PgClaimRepository`: truth's `ClaimRepository` and `ClaimUsage` (versioned, append-only)
- `write_gate_change`: a `GateChange` written in one transaction
- `LocalBlobStore`: media bytes on local disk, for tests and the demo
"""
from cortex_operator.store.adapters.audit import PgAuditStore, TenantMismatch
from cortex_operator.store.adapters.blobs import BlobError, BlobStore, LocalBlobStore
from cortex_operator.store.adapters.claims import ClaimWriteRefused, PgClaimRepository
from cortex_operator.store.adapters.gate import PgGateRepository, read_system_paused
from cortex_operator.store.adapters.uow import ChangeNotApplied, write_gate_change

__all__ = [
    "BlobError",
    "BlobStore",
    "ChangeNotApplied",
    "ClaimWriteRefused",
    "LocalBlobStore",
    "PgAuditStore",
    "PgClaimRepository",
    "PgGateRepository",
    "TenantMismatch",
    "read_system_paused",
    "write_gate_change",
]
