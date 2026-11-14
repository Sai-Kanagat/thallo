"""Write a copy outcome in one transaction: asset, bundle, variant, its claim links, its check
results and the cost ledger rows. Cost rows are written even when no asset was made (a stopped
run still spent).

The repo must offer a transaction (`repo.conn.transaction()`, as `thallo.store.TenantRepo` does)
and a `tenant_id`: without a transaction a failure halfway would leave a partial outcome, so the
write is refused, and every row must belong to the repo's tenant before anything is written."""
from __future__ import annotations

from typing import Any, Protocol

from thallo.copy.produce import CopyOutcome


class Inserter(Protocol):
    """`thallo.store.TenantRepo` satisfies this."""

    tenant_id: str
    conn: Any  # has .transaction(), a context manager

    def insert(self, row: Any) -> Any: ...


def write_outcome(repo: Inserter, outcome: CopyOutcome) -> None:
    conn = getattr(repo, "conn", None)
    if conn is None or not callable(getattr(conn, "transaction", None)):
        raise ValueError("write_outcome needs a repo with a transaction; it never writes row by row")
    tenant = getattr(repo, "tenant_id", None)
    if not isinstance(tenant, str) or not tenant:
        raise ValueError("write_outcome needs a repo scoped to one tenant")
    rows = outcome.rows()
    wrong = [type(r).__name__ for r in rows if str(r.tenant_id).lower() != tenant.lower()]
    if wrong:
        raise ValueError(f"outcome rows belong to another tenant than the repo's: {sorted(set(wrong))}")
    with conn.transaction():  # a savepoint inside the tenant transaction
        for row in rows:
            repo.insert(row)
