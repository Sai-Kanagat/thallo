-- 0041_wp33_claim_packs.sql (WP-33): where an imported industry claim came from.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- A claim pack is a signed file of manufacturer facts (thallo/extensions/packs). Importing it
-- writes ordinary claim candidates in the importing tenant; this table records, per claim, which
-- pack and which item it came from and when the pack expires. The claim itself stays a candidate
-- until a person in that tenant verifies it.
--
-- Guards:
--   * one row per (tenant, pack digest, item): the same pack cannot be imported twice
--   * no update or delete for the app role, so provenance cannot be rewritten
--   * claim_pack_expiry_guard: a verified claim that came from a pack, or any later version of
--     one, must carry an expiry no later than the pack's. So no verify path, generic or not, can
--     keep a pack fact alive past the pack, and pack claims expire together.
-- No role attributes are named, so it runs on Supabase as it does on Docker.

CREATE TABLE claim_pack_import (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  pack_id text NOT NULL CHECK (pack_id ~ '^[a-z0-9][a-z0-9-]{2,63}$'),
  pack_digest text NOT NULL CHECK (pack_digest ~ '^[0-9a-f]{64}$'),
  key_id text NOT NULL CHECK (key_id ~ '^[0-9a-f]{16}$'),
  publisher text NOT NULL CHECK (length(publisher) BETWEEN 1 AND 120),
  item_key text NOT NULL CHECK (item_key ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
  claim_id uuid NOT NULL,
  pack_expires_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, pack_digest, item_key),
  UNIQUE (tenant_id, claim_id),
  FOREIGN KEY (tenant_id, claim_id) REFERENCES claim (tenant_id, id)
);
SELECT app_enable_tenant_rls('claim_pack_import');

REVOKE UPDATE, DELETE, TRUNCATE ON claim_pack_import FROM cortex_app;

CREATE FUNCTION claim_pack_expiry_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
  limit_at timestamptz;
BEGIN
  IF NEW.state <> 'verified' THEN
    RETURN NEW;
  END IF;
  WITH RECURSIVE chain(id, supersedes_id) AS (
    SELECT NEW.id, NEW.supersedes_id
    UNION
    SELECT c.id, c.supersedes_id FROM public.claim c
      JOIN chain ON c.tenant_id = NEW.tenant_id AND c.id = chain.supersedes_id
  )
  SELECT min(p.pack_expires_at) INTO limit_at
    FROM public.claim_pack_import p JOIN chain ON p.claim_id = chain.id
   WHERE p.tenant_id = NEW.tenant_id;
  IF limit_at IS NOT NULL AND (NEW.expires_at IS NULL OR NEW.expires_at > limit_at) THEN
    RAISE EXCEPTION 'a claim from a claim pack cannot be verified past the pack expiry %', limit_at;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER claim_pack_expiry_guard BEFORE INSERT OR UPDATE ON claim
  FOR EACH ROW EXECUTE FUNCTION claim_pack_expiry_guard();
