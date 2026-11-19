"""Write a page outcome in one transaction, and read what earlier runs already claimed.

`tx` is a store `TenantRepo` (one tenant transaction; row-level security applies). The page_brief
row is unique per cluster key and per slug, so a second run can never make a second page for the
same cluster; the database refuses it even if the caller forgot to pass the existing keys."""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from thallo.extensions.pages.draft import PageOutcome


def existing(tx: Any) -> tuple[set[str], set[str]]:
    """(cluster keys, slugs) this tenant already has page briefs for."""
    rows = tx.fetch_all("SELECT cluster_key, slug FROM page_brief")
    return {k for k, _ in rows}, {s for _, s in rows}


def write_page(tx: Any, outcome: PageOutcome) -> None:
    conn = getattr(tx, "conn", None)
    if conn is None or not callable(getattr(conn, "transaction", None)):
        raise ValueError("write_page needs a repo with a transaction")
    rows = outcome.rows()
    if any(str(r.tenant_id).lower() != str(tx.tenant_id).lower() for r in rows):
        raise ValueError("outcome rows belong to another tenant than the repo's")
    with conn.transaction():
        for row in rows:
            tx.insert(row)
        rec = outcome.page_brief
        if rec is not None and outcome.variant is not None:
            tx.execute(
                "INSERT INTO page_brief (id, created_at, tenant_id, cluster_key, slug, queries, impressions,"
                " reviewer_id, variant_id, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rec["id"], tx.now(), tx.tenant_id, rec["cluster_key"], rec["slug"], Jsonb(rec["queries"]),
                 rec["impressions"], rec["reviewer_id"], rec["variant_id"], rec["status"]))
