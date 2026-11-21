"""Write a pack import in one transaction, and read what this tenant imported.

`tx` is a store `TenantRepo` (one tenant transaction; row-level security applies)."""
from __future__ import annotations

from typing import Any

from thallo.extensions.packs.importer import PackImport, PackLink

_COLS = ("id", "created_at", "tenant_id", "pack_id", "pack_digest", "key_id", "publisher", "item_key", "claim_id",
         "pack_expires_at")
_SELECT = f"SELECT {', '.join(_COLS)} FROM claim_pack_import"
_INSERT = f"INSERT INTO claim_pack_import ({', '.join(_COLS)}) VALUES ({', '.join(['%s'] * len(_COLS))})"


def write_import(tx: Any, result: PackImport) -> None:
    conn = getattr(tx, "conn", None)
    if conn is None or not callable(getattr(conn, "transaction", None)):
        raise ValueError("write_import needs a repo with a transaction")
    rows = [*result.sources, *result.claims]
    if any(str(r.tenant_id).lower() != str(tx.tenant_id).lower() for r in [*rows, *result.links]):
        raise ValueError("import rows belong to another tenant than the repo's")
    with conn.transaction():
        for row in rows:
            tx.insert(row)
        for link in result.links:
            tx.execute(_INSERT, tuple(getattr(link, c) for c in _COLS))


def _link(row: tuple[Any, ...]) -> PackLink:
    values = dict(zip(_COLS, row, strict=True))
    for k in ("id", "tenant_id", "claim_id"):
        values[k] = str(values[k])
    return PackLink(**values)


def links(tx: Any) -> list[PackLink]:
    return [_link(r) for r in tx.fetch_all(_SELECT + " ORDER BY created_at, item_key")]


def link_for(tx: Any, claim_id: str) -> PackLink | None:
    rows = tx.fetch_all(_SELECT + " WHERE claim_id = %s", (claim_id,))
    return _link(rows[0]) if rows else None


def imported_digests(tx: Any) -> set[str]:
    return {r[0] for r in tx.fetch_all("SELECT DISTINCT pack_digest FROM claim_pack_import")}
