"""Reuse the store fixtures (a migrated throwaway database and the app role)."""
from tests.wp01_store.conftest import admin, clock, database, urls  # noqa: F401
from tests.wp09_api.conftest import pg_urls, world  # noqa: F401
