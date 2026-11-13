"""Set up the Supabase database. Run it yourself; it never prints a password or URL.

    .venv/bin/python scripts/provision_db.py

1. Applies every migration with DATABASE_ADMIN_URL (the owner).
2. Creates two login roles with random passwords:
     thallo_app     member of cortex_app     (tenant-scoped app, row-level security)
     thallo_worker  member of cortex_worker  (claims jobs from the queue, nothing else)
   Running it again rotates both passwords.
3. Writes DATABASE_URL and DATABASE_WORKER_URL into .env.
4. Logs in as each role and checks it is not a superuser and cannot bypass row-level security.
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402
import psycopg  # noqa: E402
from migrate import migrate  # noqa: E402
from psycopg import sql  # noqa: E402

ROLES = {"thallo_app": ("cortex_app", "DATABASE_URL"), "thallo_worker": ("cortex_worker", "DATABASE_WORKER_URL")}


def login_url(admin_url: str, role: str, password: str) -> str:
    parts = urlsplit(admin_url)
    admin_user = parts.username or ""
    # The Supabase pooler names users "<role>.<project-ref>"; a direct connection uses the bare role.
    user = f"{role}.{admin_user.split('.', 1)[1]}" if "." in admin_user else role
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def main() -> int:
    admin_url = envfile.read().get("DATABASE_ADMIN_URL", "")
    if not admin_url.startswith(("postgresql://", "postgres://")):
        print("DATABASE_ADMIN_URL is missing or does not start with postgresql:// . Fix .env first.")
        return 1
    try:
        applied = migrate(admin_url)
    except Exception as exc:  # noqa: BLE001
        print(f"Migration failed: {type(exc).__name__}: {str(exc).replace(admin_url, '<url>')[:400]}")
        return 1
    print(f"Migrations applied: {', '.join(applied) if applied else 'none new'}")

    updates: dict[str, str] = {}
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for role, (group, env_key) in ROLES.items():
            password = secrets.token_urlsafe(32)
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            # No SUPERUSER or BYPASSRLS clause: managed Postgres forbids naming them. Both default
            # to off for a new role, and the login test below fails if either is ever on.
            conn.execute(sql.SQL(verb + " ROLE {} LOGIN INHERIT PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)))
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(role)))
            updates[env_key] = login_url(admin_url, role, password)
            print(f"{role}: {'password rotated' if exists else 'created'}, member of {group}")
    envfile.set_values(updates)
    print("DATABASE_URL and DATABASE_WORKER_URL written to .env")

    ok = True
    for role, (_group, env_key) in ROLES.items():
        try:
            with psycopg.connect(updates[env_key], connect_timeout=15) as c:
                su, bypass = c.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
                seen = c.execute("SELECT count(*) FROM tenant").fetchone()[0] if role == "thallo_app" else 0
            safe = not su and not bypass and seen == 0
            ok &= safe
            print(f"{role}: login works, superuser={su}, bypass_rls={bypass}"
                  + (f", tenants visible without a tenant set={seen}" if role == "thallo_app" else "")
                  + ("" if safe else "  <-- NOT SAFE, tell Claude"))
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"{role}: login test failed: {type(exc).__name__}: {str(exc)[:200]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
