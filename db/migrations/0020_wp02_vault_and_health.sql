-- 0020_wp02_vault_and_health.sql (WP-02): the vault and the daily health check's memory.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- The connection and channel_account tables already exist in 0001. These are the new ones.

-- ------------------------------------------------------------------ vault
-- Secrets encrypted with AES-256-GCM. Never plaintext, never the master key. The master key lives
-- only in the THALLO_VAULT_KEY environment variable. tenant_id, ref and version are bound into the
-- ciphertext as associated data, so editing any of them makes the row unreadable.
CREATE TABLE vault_secret (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  ref text NOT NULL CHECK (ref ~ '^[A-Za-z0-9_.:-]{1,128}$'),
  key_id text NOT NULL,
  version integer NOT NULL CHECK (version >= 1),
  nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
  ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) BETWEEN 17 AND 65552),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, ref)
);
SELECT app_enable_tenant_rls('vault_secret');

-- A new secret is version 1. A replacement is exactly the stored version plus one and keeps its
-- tenant and ref, so an old row cannot be written back over a newer one.
CREATE FUNCTION vault_secret_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.version <> 1 THEN
      RAISE EXCEPTION 'a new vault secret must be version 1';
    END IF;
  ELSE
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.ref <> OLD.ref THEN
      RAISE EXCEPTION 'a vault secret cannot change tenant or ref';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
      RAISE EXCEPTION 'a vault secret version must go up by exactly one';
    END IF;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER vault_secret_guard BEFORE INSERT OR UPDATE ON vault_secret
  FOR EACH ROW EXECUTE FUNCTION vault_secret_guard();

-- ------------------------------------------------------------------ health check state
CREATE TABLE connection_health (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  connection_id uuid NOT NULL,
  connected boolean NOT NULL,
  checked_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, connection_id),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connection (tenant_id, id) ON DELETE CASCADE
);
SELECT app_enable_tenant_rls('connection_health');

-- The expiry date a token.expiring warning was already sent for, so it goes out once per date.
CREATE TABLE token_warning (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  channel_account_id uuid NOT NULL,
  expires_at timestamptz NOT NULL,
  warned_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, channel_account_id),
  FOREIGN KEY (tenant_id, channel_account_id) REFERENCES channel_account (tenant_id, id) ON DELETE CASCADE
);
SELECT app_enable_tenant_rls('token_warning');

CREATE TABLE health_run (
  tenant_id uuid PRIMARY KEY REFERENCES tenant(id),
  last_run timestamptz NOT NULL
);
SELECT app_enable_tenant_rls('health_run');
