"""Postgres access with per-tenant row-level security (WP-01).

    db = Database(app_url, clock)             # app_url connects as the cortex_app role
    with db.tenant(tenant_id) as repo:        # one transaction, tenant set, RLS enforced
        repo.insert(Lead(...))
        repo.get(Lead, lead_id)

Names from the original SQLite engine store are re-exported unchanged.
"""
from cortex_operator.store.db import Database, InvalidTenant, TenantRepo
from cortex_operator.store.engine import PostgresStore
from cortex_operator.store.legacy import SCHEMA, TABLES, Store
from cortex_operator.store.rls import audit_rls

__all__ = [
    "SCHEMA",
    "TABLES",
    "Database",
    "InvalidTenant",
    "PostgresStore",
    "Store",
    "TenantRepo",
    "audit_rls",
]
