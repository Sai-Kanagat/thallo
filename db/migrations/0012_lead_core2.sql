-- 0012_lead_core2.sql: lead-owned access rules for contracts 2.0.0.
--
-- operator and identity sit above tenants. The app role gets no access at all: login and
-- the operator console reach them only through owner-run code. RLS is enabled with no
-- policy, so even an accidental grant to the app role later returns nothing. It is not
-- forced, so the owner (provisioning) can still write them.
--
-- job keeps tenant isolation for the app role: application code enqueues and reads only its
-- own tenant's jobs. The single worker claims due jobs across tenants through its OWN role,
-- cortex_worker, which can read and update job rows and nothing else. A worker process logs
-- in as a member of cortex_worker to claim, then uses the app role with app.tenant_id set
-- from job.tenant_id for the actual work. Rule: job.payload holds ids only, never content.

ALTER TABLE operator ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON operator, identity FROM cortex_app;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cortex_worker') THEN
    CREATE ROLE cortex_worker NOLOGIN NOINHERIT;
  END IF;
END
$$;
ALTER ROLE cortex_worker NOINHERIT;
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cortex_worker' AND (rolsuper OR rolbypassrls)) THEN
    RAISE EXCEPTION 'cortex_worker must not be a superuser or bypass row-level security';
  END IF;
END
$$;
GRANT USAGE ON SCHEMA public TO cortex_worker;
GRANT EXECUTE ON FUNCTION app_current_tenant() TO cortex_worker;
GRANT SELECT, UPDATE ON job TO cortex_worker;
CREATE POLICY worker_queue ON job AS PERMISSIVE FOR ALL TO cortex_worker USING (true) WITH CHECK (true);
