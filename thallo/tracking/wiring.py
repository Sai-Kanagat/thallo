"""Directories and the click recorder for the redirector.

- `PgLinkDirectory(url)`: connects with THALLO_REDIRECT_DATABASE_URL, a login that is a member of
  cortex_redirector (migration 0024) and of nothing else. That role reads no table; it can only
  call redirect_lookup(slug), which returns (id, tenant_id, destination) of a live link, so this
  connection can do nothing but turn a slug into a destination. Never the app URL (the app role cannot read across
  tenants) and never the owner URL.
- `MemoryLinkDirectory(backend)`: the same over the API's MemoryBackend, for tests and the demo.
- `backend_recorder(backend, clock)`: writes the click touch as the app role inside the link's own
  tenant transaction, after re-reading the row there and checking it is the same link.
"""
from __future__ import annotations

from typing import Any

import psycopg

from thallo.contracts.clock import Clock
from thallo.contracts.models import TrackedLink
from thallo.tracking.capture import record_click
from thallo.tracking.codes import is_slug
from thallo.tracking.redirector import ClickRecorder, PublicLink


class PgLinkDirectory:
    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("the redirector needs its own database URL")
        self.url = url

    def lookup(self, slug: str) -> PublicLink | None:
        if not is_slug(slug):
            return None
        with psycopg.connect(self.url, autocommit=True, connect_timeout=5) as conn:
            row = conn.execute("SELECT id::text, tenant_id::text, destination FROM redirect_lookup(%s)",
                               (slug,)).fetchone()
        return PublicLink(row[1], row[0], row[2]) if row else None


def is_live(rows: dict, link: TrackedLink, clock: Clock) -> bool:
    """The liveness rule of redirect_lookup (migration 0024), over in-memory tables."""
    def same(r: Any) -> bool:
        return r is not None and r.tenant_id == link.tenant_id
    if link.variant_id:
        if any(same(p) and p.variant_id == link.variant_id for p in rows.get("publication", {}).values()):
            return True
        v = rows.get("variant", {}).get(link.variant_id)
        b = rows.get("bundle", {}).get(v.bundle_id) if same(v) else None
        if same(b) and any(
                same(a) and a.subject_kind.value == "bundle" and a.subject_id == b.id and a.voided_at is None
                and a.expires_at > clock.now() and a.content_hash == b.root_hash
                for a in rows.get("approval", {}).values()):
            return True
    if link.ad_id:
        d = rows.get("ad", {}).get(link.ad_id)
        if same(d) and d.state.value in ("active", "paused"):
            return True
    return False


class MemoryLinkDirectory:
    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def lookup(self, slug: str) -> PublicLink | None:
        if not is_slug(slug):
            return None
        rows = self.backend.tables.rows
        for row in rows.get("tracked_link", {}).values():
            if row.slug == slug:
                live = is_live(rows, row, self.backend.clock)
                return PublicLink(row.tenant_id, row.id, row.destination) if live else None
        return None


class LinkChanged(RuntimeError):
    pass


def backend_recorder(backend: Any, clock: Clock) -> ClickRecorder:
    def record(link: PublicLink) -> None:
        with backend.tenant(link.tenant_id) as tx:
            row = tx.get(TrackedLink, link.link_id)
            if row is None or row.tenant_id != link.tenant_id or row.destination != link.destination:
                raise LinkChanged("tracked link does not match its tenant row")
            record_click(tx, clock, row)
    return record


__all__ = ["LinkChanged", "MemoryLinkDirectory", "PgLinkDirectory", "backend_recorder"]
