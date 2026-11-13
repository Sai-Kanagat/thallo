-- 0011_wp01_hardening.sql (WP-01): hardening after review. 0010 stays as applied.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.

-- Pin the search_path of the helpers so a caller cannot shadow a function or operator.
-- The policy helper is schema-qualified inside app_enable_tenant_rls for the same reason.
CREATE OR REPLACE FUNCTION app_enable_tenant_rls(tbl regclass, col name DEFAULT 'tenant_id') RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tbl);
  EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tbl);
  EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', tbl);
  EXECUTE format(
    'CREATE POLICY tenant_isolation ON %s AS PERMISSIVE FOR ALL '
    'USING (%I = public.app_current_tenant()) WITH CHECK (%I = public.app_current_tenant())', tbl, col, col);
  EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %s TO cortex_app', tbl);
END
$$;

ALTER FUNCTION app_current_tenant() SET search_path = pg_catalog;

-- The app role can never be privileged, whatever else is granted to it.
-- Managed Postgres (Supabase) has no true superuser, and only a superuser may name the
-- SUPERUSER attribute at all, so it is checked here instead of set.
ALTER ROLE cortex_app NOINHERIT;
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cortex_app' AND (rolsuper OR rolbypassrls)) THEN
    RAISE EXCEPTION 'cortex_app must not be a superuser or bypass row-level security';
  END IF;
END
$$;

-- The kill switch is operator-only: the app role reads its tenant row and nothing more.
REVOKE UPDATE (paused) ON tenant FROM cortex_app;
REVOKE UPDATE ON tenant FROM cortex_app;

-- Supabase hands anon and authenticated broad table rights by default. Neither role is
-- part of this design, so take the rights away and stop future tables from getting them.
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', r);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', r);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', r);
    END IF;
  END LOOP;
END
$$;
