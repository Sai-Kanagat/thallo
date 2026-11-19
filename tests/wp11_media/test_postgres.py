"""Migration 0029 on a real Postgres: media_file is tenant-isolated and append-only."""
from __future__ import annotations

import secrets

import psycopg
import pytest

from tests.wp09_api.conftest import pg_urls  # noqa: F401
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Capability, Language
from thallo.contracts.ids import new_id
from thallo.contracts.models import Operator, Tenant
from thallo.media.ingest import MediaFile, ingest_upload

from .conftest import png


def test_media_file_rows_are_tenant_scoped_and_append_only(pg_urls, tmp_path, registry):  # noqa: F811
    from api.media_store import TenantMediaStore
    from thallo.store import Database

    clock = FakeClock(FAKE_EPOCH)
    db = Database(pg_urls["app"], clock, admin_url=pg_urls["admin"])
    ids = []
    for name in ("A", "B"):
        op = Operator(id=new_id(clock), created_at=clock.now(), name=f"op {name}")
        t = Tenant(id=new_id(clock), created_at=clock.now(), operator_id=op.id, name=name, currency="EUR",
                   slug=f"{name.lower()}-{secrets.token_hex(3)}", default_language=Language.IT, region_code="IT")
        db.create_operator(op)
        db.create_tenant(t)
        ids.append(t.id)
    a, b = ids
    root = tmp_path / "root"
    root.mkdir()
    blobs = TenantMediaStore(root)
    with db.tenant(a) as tx:
        for row in registry.ensure_manual(a, Capability.IMAGE):  # in production the store holds the rows
            tx.insert(row)
        got = ingest_upload(tx, blobs, registry, tenant_id=a, data=png(), mime="image/png", clock=clock)
    with db.tenant(a) as tx:
        assert tx.get(MediaFile, got.file.id).media_id == got.media.id
    with db.tenant(b) as tx:
        assert tx.get(MediaFile, got.file.id) is None
    with pytest.raises(psycopg.Error), db.tenant(a) as tx:
        tx.execute("UPDATE media_file SET cost_eur = 1")
