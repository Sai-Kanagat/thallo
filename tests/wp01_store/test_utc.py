"""Times come back in UTC whatever the server or role time zone is (contracts: "Times are
timezone-aware UTC"). The gate compares an approval's `approved_at.isoformat()` with the signed
grant, so a time read back as +01:00 would block every publish."""
from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from thallo.store import Database


def test_times_read_back_in_utc_under_a_non_utc_session(urls, clock, tenants):
    params = conninfo_to_dict(urls["app"])
    params["options"] = "-c TimeZone=Europe/Rome"
    rome = Database(make_conninfo(**params), clock)
    now = clock.now()
    with rome.tenant(tenants[0].id) as repo:
        value = repo.execute("SELECT %s::timestamptz", (now,)).fetchone()[0]
        created = repo.execute("SELECT created_at FROM tenant").fetchone()[0]
    assert value.isoformat() == now.isoformat()
    assert value.utcoffset().total_seconds() == 0
    assert created.utcoffset().total_seconds() == 0
