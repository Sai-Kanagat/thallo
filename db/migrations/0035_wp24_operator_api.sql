-- 0035_wp24_operator_api.sql (WP-24): operator membership and API tokens.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- Both tables sit above tenant scope, like operator and identity (0012): RLS enabled with no
-- policy and no grant to the app role. Only owner-run code (api/operators.py, api/tokens.py)
-- reads or writes them. A token is looked up by its hash before any tenant is known, so the
-- lookup cannot run as the app role; the tenant it names is then used for the app-role work.
--
-- operator_member  which identities run which operator. The operator console shows the tenants
--                  of those operators and nothing else.
-- api_token        a per-tenant token for scripts and agents. Only the SHA-256 of the token is
--                  kept. Scopes are read and draft only: approval, claim verification, sending,
--                  publishing and spend signing are refused to every token by the API.
-- No role attributes are named, so this runs on Supabase as on Docker.

CREATE TABLE operator_member (
  operator_id uuid NOT NULL REFERENCES operator(id),
  identity_id uuid NOT NULL REFERENCES identity(id),
  created_at timestamptz NOT NULL,
  PRIMARY KEY (operator_id, identity_id)
);
CREATE INDEX operator_member_identity_idx ON operator_member (identity_id);
ALTER TABLE operator_member ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON operator_member FROM PUBLIC;
REVOKE ALL ON operator_member FROM cortex_app;

CREATE TABLE api_token (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  app_user_id uuid NOT NULL,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
  prefix text NOT NULL CHECK (length(prefix) BETWEEN 4 AND 16),
  scopes text[] NOT NULL CHECK (cardinality(scopes) >= 1 AND scopes <@ ARRAY['read', 'draft']::text[]),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  revoked_by uuid,
  last_used_at timestamptz,
  CHECK (expires_at > created_at),
  CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
  FOREIGN KEY (tenant_id, app_user_id) REFERENCES app_user (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX api_token_tenant_idx ON api_token (tenant_id);
ALTER TABLE api_token ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON api_token FROM PUBLIC;
REVOKE ALL ON api_token FROM cortex_app;
