-- 0013_wp01_system_setting.sql (WP-01 follow-up): the global kill switch the gate reads.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- One row, above tenants. The app role may read it and nothing more: only the owner (the
-- operator console, owner-run code) flips it. Row-level security is enabled with a read-only
-- policy for the app role and is not forced, so the owner can still write. The gate reads a
-- missing row, or any value but false, as paused (fail closed). store/rls.py audits this table
-- as GLOBAL_READ_ONLY: SELECT policies only, and SELECT the only grant to cortex roles.

CREATE TABLE system_setting (
  id boolean PRIMARY KEY DEFAULT true CHECK (id),
  system_paused boolean NOT NULL
);

INSERT INTO system_setting (id, system_paused) VALUES (true, false);

ALTER TABLE system_setting ENABLE ROW LEVEL SECURITY;
CREATE POLICY read_only ON system_setting AS PERMISSIVE FOR SELECT TO cortex_app USING (true);
REVOKE ALL ON system_setting FROM cortex_app;
GRANT SELECT ON system_setting TO cortex_app;
