-- 0010_wp01_rls.sql (WP-01): row-level security per tenant.
--
-- Model
--   * cortex_app is the application role. It is not a superuser, does not bypass RLS and
--     cannot log in until the operator gives it a login and a password (never in this file).
--   * Every request runs in one transaction that begins with
--         SELECT set_config('app.tenant_id', '<uuid>', true)      -- same as SET LOCAL
--     app_current_tenant() reads it. No setting, or an empty one, means no rows at all.
--   * Every table has ENABLE and FORCE ROW LEVEL SECURITY and one policy, tenant_isolation,
--     for all commands: USING and WITH CHECK both require the row's tenant to be the
--     current one. A tenant cannot read, insert, update or delete another tenant's row,
--     and cannot move a row to another tenant.
--   * FORCE makes the table owner obey the policy too. Only a superuser or a role with
--     BYPASSRLS skips it, and cortex_app is neither.
--
-- Future migrations: after CREATE TABLE of a table with tenant_id, call
--     SELECT app_enable_tenant_rls('my_table');
-- A test (tests/wp01_store) fails if any table is left without its policy.
-- scripts/migrate.py wraps this file in a transaction: no BEGIN or COMMIT here.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cortex_app') THEN
    CREATE ROLE cortex_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO cortex_app;

CREATE FUNCTION app_current_tenant() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;

GRANT EXECUTE ON FUNCTION app_current_tenant() TO cortex_app;

CREATE FUNCTION app_enable_tenant_rls(tbl regclass, col name DEFAULT 'tenant_id') RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tbl);
  EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tbl);
  EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', tbl);
  EXECUTE format(
    'CREATE POLICY tenant_isolation ON %s AS PERMISSIVE FOR ALL '
    'USING (%I = app_current_tenant()) WITH CHECK (%I = app_current_tenant())', tbl, col, col);
  EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %s TO cortex_app', tbl);
END
$$;

REVOKE ALL ON FUNCTION app_enable_tenant_rls(regclass, name) FROM PUBLIC;

-- ------------------------------------------------------------------ engine state tables
-- The original engine's durable state (passports, drafts, claims, taste, forecast ledger,
-- metrics, progress), one set of rows per tenant. These are WP-01's own tables.
-- Times are ISO text: the original engine uses naive datetimes and they must round-trip exactly.
-- The porting packages move to the core tables and timestamptz.

CREATE TABLE engine_passport (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  item_id text NOT NULL,
  approver text NOT NULL,
  approved_at text NOT NULL,
  content_hash text NOT NULL,
  rule_version integer NOT NULL,
  claim_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (tenant_id, item_id)
);

CREATE TABLE engine_draft (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  item_id text NOT NULL,
  body text NOT NULL,
  PRIMARY KEY (tenant_id, item_id)
);

CREATE TABLE engine_claim (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  claim_id text NOT NULL,
  body text NOT NULL,
  source text NOT NULL,
  evidence_hash text NOT NULL,
  expires_at text,
  verified boolean NOT NULL DEFAULT false,
  PRIMARY KEY (tenant_id, claim_id)
);

CREATE TABLE engine_taste (
  tenant_id uuid PRIMARY KEY REFERENCES tenant(id),
  body jsonb NOT NULL
);

CREATE TABLE engine_entry (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  seq integer NOT NULL,
  at text NOT NULL,
  previous text NOT NULL,
  digest text NOT NULL,
  PRIMARY KEY (tenant_id, seq)
);

-- Sealed payloads and scores are text, not jsonb: the chain hashes the exact payload, and
-- text keeps it byte for byte.
CREATE TABLE engine_sealed (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  seq integer NOT NULL,
  payload text NOT NULL,
  PRIMARY KEY (tenant_id, seq)
);

CREATE TABLE engine_score (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  seq integer NOT NULL,
  result text NOT NULL,
  PRIMARY KEY (tenant_id, seq)
);

CREATE TABLE engine_metric (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  n integer NOT NULL,
  brand text NOT NULL,
  channel text NOT NULL,
  item_id text NOT NULL,
  day integer NOT NULL,
  name text NOT NULL,
  value double precision NOT NULL,
  pulled_at text NOT NULL,
  PRIMARY KEY (tenant_id, n)
);

CREATE TABLE engine_progress (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  item_id text NOT NULL,
  forecast_seq integer,
  metrics_pulled jsonb NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (tenant_id, item_id)
);

-- ------------------------------------------------------------------ apply to every table
DO $$
DECLARE
  t record;
BEGIN
  FOR t IN
    SELECT c.oid::regclass AS tbl
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND c.relname NOT IN ('tenant', 'schema_migrations')
      AND EXISTS (SELECT FROM pg_attribute a
                  WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)
  LOOP
    PERFORM app_enable_tenant_rls(t.tbl);
  END LOOP;
END
$$;

-- The tenant table is scoped by its own id. The app role may read its row and flip
-- the kill switch; creating, renaming or deleting tenants is provisioning, done by the owner.
SELECT app_enable_tenant_rls('tenant', 'id');
REVOKE INSERT, UPDATE, DELETE ON tenant FROM cortex_app;
GRANT UPDATE (paused) ON tenant TO cortex_app;

-- Ledgers are append-only for the app role.
REVOKE UPDATE, DELETE ON audit_log, event_log, cost_ledger FROM cortex_app;
