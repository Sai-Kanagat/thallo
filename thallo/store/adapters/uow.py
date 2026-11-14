"""Write a gate change in one transaction (gate/AUDIT.md, "One transaction per change").

    with database.tenant(tenant_id) as repo:
        edit_the_variant(repo)                 # the edit that caused a void, if any
        write_gate_change(repo, change)        # approval rows, bundle, variants, audit entries

`write_gate_change` runs inside a savepoint of the tenant transaction: if any step fails (an
audit seq taken by another writer, a row hidden by row-level security, a constraint), every
step of the change rolls back and the error is raised, so the caller's transaction rolls back
too unless it catches it. The audit entries go in last, in order, after the rows they describe.
"""
from __future__ import annotations

from thallo.contracts.models import Approval, Bundle, Variant
from thallo.gate.approvals import GateChange
from thallo.store.adapters.audit import PgAuditStore, own
from thallo.store.db import TenantRepo


class ChangeNotApplied(RuntimeError):
    """A row the change updates was not found for this tenant. Nothing was written."""


def _update(repo: TenantRepo, model, row_id: str, **fields) -> None:
    if not repo.update(model, row_id, **fields):
        raise ChangeNotApplied(f"{model.__table__} {row_id} not found for this tenant")


def write_gate_change(repo: TenantRepo, change: GateChange) -> None:
    own(repo, change.tenant_id)
    audit = PgAuditStore(repo)
    with repo.conn.transaction():
        if change.new_approval is not None:
            own(repo, change.new_approval.tenant_id)
            repo.insert(change.new_approval)
        for a in change.voided_approvals:
            own(repo, a.tenant_id)
            _update(repo, Approval, a.id, voided_at=a.voided_at, void_reason=a.void_reason)
        own(repo, change.bundle.tenant_id)
        _update(repo, Bundle, change.bundle.id, state=change.bundle.state, root_hash=change.bundle.root_hash)
        for v in change.variants:
            own(repo, v.tenant_id)
            _update(repo, Variant, v.id, state=v.state, content_sha256=v.content_sha256)
        for entry in change.audit_entries:
            audit.insert(entry)


__all__ = ["ChangeNotApplied", "write_gate_change"]
