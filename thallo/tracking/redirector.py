"""The public redirector behind THALLO_REDIRECT_PREFIXES: `<prefix><slug>` to the stored destination.

Safety rules, each with a test (tests/wp12_tracking):
- Never an open redirect. The only Location ever sent is the destination stored on the tracked link
  for that slug, byte for byte, re-checked by `check_destination` at redirect time. Nothing from the
  request (query string, headers, path beyond the slug) reaches the response. A slug that is not
  10 base32 characters is refused before any lookup. Unknown slug: 404, no redirect.
- Only live links: the directory returns a link only when its variant is published or holds a
  live approval on the current root hash, or its ad is active or paused (redirect_lookup,
  migration 0024). A minted, unapproved, voided or source-less link is a 404.
- The same destination the gate trusts: `PgGateRepository.resolve_link` reads the same row for the
  same `<prefix><slug>` shape, so the reviewer approved exactly where the browser is sent.
- Privacy-first counting: no cookie, no IP stored or logged, no fingerprint kept. The client address
  is used only in memory, hashed with a per-process random salt, for the rate limit and to count a
  repeat click from the same client on the same link within 30 minutes once. HEAD is never counted.
- Rate limited per client, per network and per link, with fixed in-memory windows.
- Counting is best effort: if the touch cannot be written the visitor is still redirected (the link
  was approved), and the failure is logged without any request data.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from thallo.contracts.clock import Clock
from thallo.tracking.codes import is_slug
from thallo.tracking.destinations import BadDestination, check_destination

log = logging.getLogger(__name__)

PER_CLIENT = 60  # requests per window
PER_NETWORK = 600
PER_LINK = 3000
WINDOW = timedelta(minutes=1)
REPEAT_WINDOW = timedelta(minutes=30)
MAX_REPEAT_KEYS = 100_000


@dataclass(frozen=True)
class PublicLink:
    tenant_id: str
    link_id: str
    destination: str


class LinkDirectory(Protocol):
    """Slug to (tenant, link, destination) across tenants. The only cross-tenant read; backed by
    the cortex_redirector role (migration 0024), which can only call redirect_lookup(slug)."""

    def lookup(self, slug: str) -> PublicLink | None: ...


ClickRecorder = Callable[[PublicLink], None]  # writes the touch inside that tenant's transaction


@dataclass(frozen=True)
class Outcome:
    status: int  # 302, 404, 429 or 503
    location: str | None = None
    counted: bool = False


class _Windows:
    """Fixed-window counters. The whole table is dropped when the window rolls over, so it stays
    bounded by one window of traffic."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._start = clock.now()
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        with self._lock:
            now = self.clock.now()
            if now - self._start >= WINDOW:
                self._start, self._counts = now, {}
            n = self._counts.get(key, 0)
            if n >= limit:
                return False
            self._counts[key] = n + 1
            return True


class Redirector:
    def __init__(self, directory: LinkDirectory, record: ClickRecorder, clock: Clock) -> None:
        self.directory = directory
        self.record = record
        self.clock = clock
        self._salt = secrets.token_bytes(16)
        self._windows = _Windows(clock)
        self._seen: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()

    def _h(self, value: str) -> str:
        return hashlib.blake2b(value.encode(), key=self._salt, digest_size=16).hexdigest()

    def _first_click(self, client: str, slug: str) -> bool:
        key, now = self._h(f"{client}|{slug}"), self.clock.now()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < REPEAT_WINDOW:  # type: ignore[operator]
                return False
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > MAX_REPEAT_KEYS:
                self._seen.popitem(last=False)
            return True

    def handle(self, slug: str, *, client: str, network: str, count: bool = True) -> Outcome:
        """`client` and `network` are opaque keys for the caller's address (never stored)."""
        if not is_slug(slug):
            return Outcome(404)
        if not (self._windows.allow("c:" + self._h(client), PER_CLIENT)
                and self._windows.allow("n:" + self._h(network), PER_NETWORK)
                and self._windows.allow("l:" + slug, PER_LINK)):
            return Outcome(429)
        try:
            link = self.directory.lookup(slug)
        except Exception:
            log.exception("tracked link lookup failed")
            return Outcome(503)
        if link is None:
            return Outcome(404)
        try:
            location = check_destination(link.destination)
        except BadDestination:
            log.error("tracked link %s has a destination that fails the check; not redirecting", link.link_id)
            return Outcome(404)
        if location != link.destination:
            log.error("tracked link %s destination is not in normal form; not redirecting", link.link_id)
            return Outcome(404)
        counted = False
        if count and self._first_click(client, slug):
            try:
                self.record(link)
                counted = True
            except Exception:
                log.exception("could not count a click on tracked link %s", link.link_id)
        return Outcome(302, location, counted)


__all__ = ["ClickRecorder", "LinkDirectory", "Outcome", "PublicLink", "Redirector"]
