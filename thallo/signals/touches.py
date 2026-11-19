"""The own-logs reader: counts of this tenant's `touch` rows per day and kind (WP-12 writes them).
Runs inside the caller's tenant transaction, so row-level security limits it to that tenant."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from thallo.signals.context import TouchReader


def reader(tx: Any) -> TouchReader:
    def read(start: date, end: date) -> list[tuple[date, str, int]]:
        rows = tx.fetch_all(
            "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS d, kind, count(*) FROM touch"
            " WHERE occurred_at >= %s AND occurred_at < %s GROUP BY 1, 2 ORDER BY 1, 2",
            (start, end + timedelta(days=1)))
        return [(d, k, int(n)) for d, k, n in rows]
    return read
