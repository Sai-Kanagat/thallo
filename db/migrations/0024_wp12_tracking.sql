-- 0024_wp12_tracking.sql (WP-12): link and touch tracking.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- 1. tracking_code: a tenant's offer, walk-in, call and QR codes (thallo/tracking/models.py).
--    A WhatsApp reference code is not here: it lives on tracked_link.whatsapp_code, which the
--    gate already hashes into the variant.
-- 2. tracked_link is fixed once written: slug, destination, code, variant and ad cannot change,
--    so a link in a published post can never be repointed. A new destination is a new row.
-- 3. cortex_redirector: the public redirector's own role, following cortex_worker in 0012. It
--    can read four columns of tracked_link across tenants and nothing else, so a click can be
--    turned into (tenant, link, destination). The click touch is then written by the app role
--    inside that tenant's transaction. It is NOLOGIN; a redirector process logs in as a member.
-- No role attributes beyond those of 0012 are named, so it runs on Supabase as on Docker.

CREATE TABLE tracking_code (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  code text NOT NULL CHECK (code ~ '^[A-Z0-9]{4,12}$'),
  kind text NOT NULL CHECK (kind IN ('walk_in', 'call', 'offer', 'qr')),
  variant_id uuid,
  ad_id uuid,
  tracked_link_id uuid,
  label text CHECK (label IS NULL OR length(label) <= 120),
  active boolean NOT NULL DEFAULT true,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, code),
  CHECK (kind <> 'qr' OR tracked_link_id IS NOT NULL),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, ad_id) REFERENCES ad (tenant_id, id),
  FOREIGN KEY (tenant_id, tracked_link_id) REFERENCES tracked_link (tenant_id, id)
);
CREATE INDEX tracking_code_tenant_idx ON tracking_code (tenant_id);
SELECT app_enable_tenant_rls('tracking_code');

CREATE FUNCTION tracked_link_immutable() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  IF NEW.slug IS DISTINCT FROM OLD.slug OR NEW.destination IS DISTINCT FROM OLD.destination
     OR NEW.whatsapp_code IS DISTINCT FROM OLD.whatsapp_code OR NEW.variant_id IS DISTINCT FROM OLD.variant_id
     OR NEW.ad_id IS DISTINCT FROM OLD.ad_id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION 'tracked_link is fixed at insert; mint a new link'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION tracked_link_immutable() FROM PUBLIC;
CREATE TRIGGER tracked_link_immutable BEFORE UPDATE ON tracked_link
  FOR EACH ROW EXECUTE FUNCTION tracked_link_immutable();

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cortex_redirector') THEN
    CREATE ROLE cortex_redirector NOLOGIN NOINHERIT;
  END IF;
END
$$;
ALTER ROLE cortex_redirector NOINHERIT;
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cortex_redirector' AND (rolsuper OR rolbypassrls)) THEN
    RAISE EXCEPTION 'cortex_redirector must not be a superuser or bypass row-level security';
  END IF;
END
$$;
GRANT USAGE ON SCHEMA public TO cortex_redirector;
GRANT SELECT (id, tenant_id, slug, destination) ON tracked_link TO cortex_redirector;
CREATE POLICY redirector_lookup ON tracked_link AS PERMISSIVE FOR SELECT TO cortex_redirector USING (true);
