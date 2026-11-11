"""Audit that every table is behind row-level security. Usable from any package's tests.

    problems = audit_rls(conn)   # [] when every public table is protected

A table passes when row-level security is enabled and forced, it has a policy for all
commands, and both the read filter and the write check compare against app_current_tenant().
"""
from __future__ import annotations

import psycopg

EXEMPT = frozenset({"schema_migrations"})  # migration bookkeeping, no grants to the app role

_QUERY = """
SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
       coalesce(array_agg(p.polcmd::text || '|' ||
                          coalesce(pg_get_expr(p.polqual, p.polrelid), '') || '|' ||
                          coalesce(pg_get_expr(p.polwithcheck, p.polrelid), ''))
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
        if not enabled:
            problems.append(f"{name}: row-level security not enabled")
        if not forced:
            problems.append(f"{name}: row-level security not forced (owner would bypass it)")
        full = [p.split("|", 2) for p in policies if p.startswith("*|")]
        if not full:
            problems.append(f"{name}: no policy for all commands")
        for _cmd, using, check in full:
            if "app_current_tenant()" not in using or "app_current_tenant()" not in check:
                problems.append(f"{name}: policy does not filter and check on app_current_tenant()")
    return problems
