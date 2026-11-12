"""Audit that every table is behind row-level security. Usable from any package's tests.

    problems = audit_rls(conn)   # [] when every public table is protected

A table passes when row-level security is enabled and forced, it has a policy for all
commands, and both the read filter and the write check compare against app_current_tenant().
"""
from __future__ import annotations

import psycopg

EXEMPT = frozenset({"schema_migrations"})  # migration bookkeeping, no grants to the app role
# Above tenants: RLS enabled with no policy, and no grants to the app role (0012).
NO_ACCESS = frozenset({"operator", "identity"})
# Narrow cross-tenant policies allowed to non-app roles only: (table, policy, role).
WORKER_POLICIES = frozenset({("job", "worker_queue", "cortex_worker")})

_QUERY = """
SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
       coalesce(array_agg(p.polcmd::text || '|' ||
                          coalesce(pg_get_expr(p.polqual, p.polrelid), '') || '|' ||
                          coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '') || '|' ||
                          p.polname || '|' ||
                          coalesce((SELECT string_agg(CASE WHEN r = 0 THEN 'public' ELSE r::regrole::text END, ',')
                                    FROM unnest(p.polroles) AS r), 'public'))
                FILTER (WHERE p.polname IS NOT NULL), '{}')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_policy p ON p.polrelid = c.oid
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity
ORDER BY c.relname
"""


def audit_rls(conn: psycopg.Connection) -> list[str]:
    problems: list[str] = []
    for name, enabled, forced, policies in conn.execute(_QUERY).fetchall():
        if name in EXEMPT:
            continue
        if name in NO_ACCESS:
            if not enabled or policies:
                problems.append(f"{name}: must have RLS enabled with no policy")
            granted = conn.execute(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE table_name = %s AND grantee LIKE 'cortex_app%%'", (name,)).fetchone()[0]
            if granted:
                problems.append(f"{name}: the app role must have no grants")
            continue
        if not enabled:
            problems.append(f"{name}: row-level security not enabled")
        if not forced:
            problems.append(f"{name}: row-level security not forced (owner would bypass it)")
        parsed = [p.split("|", 4) for p in policies]
        tenant_scoped = [x for x in parsed if x[0] == "*" and x[3] == "tenant_isolation"]
        if not tenant_scoped:
            problems.append(f"{name}: no tenant_isolation policy for all commands")
        for cmd, using, check, polname, roles in parsed:
            if (name, polname, roles) in WORKER_POLICIES:
                continue  # narrow, named, granted to a non-app role only
            writes = cmd in ("*", "a", "w")
            if "app_current_tenant()" not in using or (writes and "app_current_tenant()" not in check):
                problems.append(f"{name}: policy {polname} for {roles} does not filter and check on "
                                "app_current_tenant()")
    return problems
