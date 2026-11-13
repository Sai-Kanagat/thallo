"""The audit log's `AuditStore` over Postgres (WP-01 follow-up to WP-04).

Insert only. The app role has no UPDATE or DELETE on audit_log (migration 0010), and this class
offers neither. Two writers that prepared the same next seq race on the unique (tenant_id, seq):
the second insert fails and becomes `AuditConflict`. The insert runs in a savepoint, so the
caller's transaction is still usable to roll back or retry; inside `write_gate_change` the
conflict aborts the whole change.
"""
from __future__ import annotations

from collections.abc import Sequence

from psycopg import errors

from cortex_operator.contracts.models import AuditEntry
from cortex_operator.store.db import TenantRepo
from cortex_operator.trust.audit import AuditConflict, AuditError


class TenantMismatch(PermissionError):
    """A call named a tenant other than the one this transaction is scoped to."""


def own(repo: TenantRepo, tenant_id: str) -> None:
    if tenant_id != repo.tenant_id:
        raise TenantMismatch("this repository is scoped to another tenant")


class PgAuditStore:
    def __init__(self, repo: TenantRepo) -> None:
        self.repo = repo

    def head(self, tenant_id: str) -> AuditEntry | None:
        own(self.repo, tenant_id)
        rows = self.repo.query(AuditEntry, "SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1")
        return rows[0] if rows else None

    def insert(self, entry: AuditEntry) -> None:
        own(self.repo, entry.tenant_id)
        try:
            with self.repo.conn.transaction():  # a savepoint: a conflict leaves the outer transaction usable
                self.repo.insert(entry)
        except errors.UniqueViolation as exc:
            raise AuditConflict(f"seq {entry.seq} already exists for this tenant") from exc

    def entries(self, tenant_id: str) -> Sequence[AuditEntry]:
        return self.since(tenant_id, 1)

    def since(self, tenant_id: str, from_seq: int) -> Sequence[AuditEntry]:
        own(self.repo, tenant_id)
        if not isinstance(from_seq, int) or isinstance(from_seq, bool):
            raise AuditError("from_seq must be an int")
        return tuple(self.repo.query(AuditEntry, "SELECT * FROM audit_log WHERE seq >= %s ORDER BY seq",
                                     (from_seq,)))

    def about(self, tenant_id: str, subject_kind: str, subject_id: str) -> Sequence[AuditEntry]:
        own(self.repo, tenant_id)
        return tuple(self.repo.query(
            AuditEntry, "SELECT * FROM audit_log WHERE subject_kind = %s AND subject_id = %s ORDER BY seq",
            (subject_kind, subject_id)))


__all__ = ["PgAuditStore", "TenantMismatch", "own"]
