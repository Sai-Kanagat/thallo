"""Postgres access with per-tenant row-level security (WP-01).

    db = Database(app_url, clock)             # app_url connects as the cortex_app role
    with db.tenant(tenant_id) as repo:        # one transaction, tenant set, RLS enforced
        repo.insert(Lead(...))
        repo.get(Lead, lead_id)

Names from the original SQLite engine store are re-exported unchanged.

Rules for callers
- `repo.execute()` runs raw SQL. It is for trusted code only: row-level security still limits
  it to the tenant, but it is the one place a hand-written statement can be wrong. Prefer the
  typed methods.
- Every new table with a tenant_id calls `SELECT app_enable_tenant_rls('table');` in its
  migration. `audit_rls()` and the tests fail if one is missed.
- Some lookups must cross tenants before the tenant is known (a redirect slug, a WhatsApp
  reference code). Never solve that with a BYPASSRLS role or by switching the app role off RLS.
  Use a narrow SECURITY DEFINER resolver that takes one key, returns only what the caller
  needs, and pins its search_path (`SET search_path = pg_catalog`). WP-12 builds it.
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
