"""WP-10 fixtures. The API tests reuse WP-09's two-tenant world (memory and Postgres)."""
from __future__ import annotations

from tests.wp09_api.conftest import pg_urls, world  # noqa: F401

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 60 + b"hand-made audio bytes for tests"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
