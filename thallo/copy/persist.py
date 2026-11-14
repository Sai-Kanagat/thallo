"""Write a copy outcome in one transaction: asset, bundle, variant, its claim links, its check
results and the cost ledger rows. Cost rows are written even when no asset was made (a stopped
run still spent)."""
from __future__ import annotations

from typing import Any, Protocol

from thallo.copy.produce import CopyOutcome


class Inserter(Protocol):
    """`thallo.store.TenantRepo` satisfies this."""

    def insert(self, row: Any) -> Any: ...


def write_outcome(repo: Inserter, outcome: CopyOutcome) -> None:
    rows = outcome.rows()
    tenants = {r.tenant_id for r in rows}
    if len(tenants) > 1:
        raise ValueError("an outcome may only hold one tenant's rows")
    conn = getattr(repo, "conn", None)
    if conn is not None:
        with conn.transaction():  # a savepoint inside the tenant transaction
            for row in rows:
                repo.insert(row)
    else:
        for row in rows:
            repo.insert(row)
