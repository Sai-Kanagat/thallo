"""Reuse the store fixtures (a migrated throwaway database and the app role)."""
from tests.wp01_store.conftest import admin, clock, database, urls  # noqa: F401
