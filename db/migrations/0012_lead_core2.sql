-- 0012_lead_core2.sql: lead-owned access rules for contracts 2.0.0.
--
-- operator and identity sit above tenants. The app role gets no access at all: login and
-- the operator console reach them only through owner-run code. RLS is enabled with no
-- policy, so even an accidental grant to the app role later returns nothing. It is not
-- forced, so the owner (provisioning) can still write them.
--
-- job is the one cross-tenant queue: the single worker must see every tenant's due jobs.
-- Rule (docs/contracts.md): job.payload holds ids only, never client content. The worker
-- claims a job here, then sets app.tenant_id from job.tenant_id before touching tenant data.

ALTER TABLE operator ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON operator, identity FROM cortex_app;

DROP POLICY IF EXISTS tenant_isolation ON job;
ALTER TABLE job NO FORCE ROW LEVEL SECURITY;
ALTER TABLE job DISABLE ROW LEVEL SECURITY;
REVOKE ALL ON job FROM cortex_app;
GRANT SELECT, INSERT, UPDATE ON job TO cortex_app;
