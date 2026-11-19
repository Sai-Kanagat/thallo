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

-- 4. Only live links redirect (lead review). tracked_link_live is the one relation the redirector
--    queries: a link whose variant is published, or whose bundle holds an approval that is not
--    voided, not expired and bound to the bundle's current root hash; or whose ad is active or
--    paused (an ad goes active only after a signed spend proposal). A link with neither a variant
--    nor an ad never redirects. The view runs with the caller's rights (security_invoker), so
--    row-level security still applies; the redirector role gets SELECT-only policies and grants on
--    just the columns the view reads. A published link keeps working after a later void: the post
--    is already out.
GRANT SELECT (variant_id, ad_id) ON tracked_link TO cortex_redirector;
GRANT SELECT (id, tenant_id, bundle_id) ON variant TO cortex_redirector;
GRANT SELECT (id, tenant_id, root_hash) ON bundle TO cortex_redirector;
GRANT SELECT (tenant_id, subject_kind, subject_id, content_hash, expires_at, voided_at) ON approval TO cortex_redirector;
GRANT SELECT (tenant_id, variant_id) ON publication TO cortex_redirector;
GRANT SELECT (id, tenant_id, state) ON ad TO cortex_redirector;
CREATE POLICY redirector_lookup ON variant AS PERMISSIVE FOR SELECT TO cortex_redirector USING (true);
CREATE POLICY redirector_lookup ON bundle AS PERMISSIVE FOR SELECT TO cortex_redirector USING (true);
CREATE POLICY redirector_lookup ON approval AS PERMISSIVE FOR SELECT TO cortex_redirector USING (true);
CREATE POLICY redirector_lookup ON publication AS PERMISSIVE FOR SELECT TO cortex_redirector USING (true);
CREATE POLICY redirector_lookup ON ad AS PERMISSIVE FOR SELECT TO cortex_redirector USING (true);

CREATE VIEW tracked_link_live WITH (security_invoker = true) AS
SELECT l.id, l.tenant_id, l.slug, l.destination
FROM tracked_link l
WHERE (l.variant_id IS NOT NULL AND (
         EXISTS (SELECT 1 FROM publication p WHERE p.tenant_id = l.tenant_id AND p.variant_id = l.variant_id)
         OR EXISTS (SELECT 1 FROM variant v
                    JOIN bundle b ON b.tenant_id = v.tenant_id AND b.id = v.bundle_id
                    JOIN approval a ON a.tenant_id = b.tenant_id AND a.subject_kind = 'bundle' AND a.subject_id = b.id
                    WHERE v.tenant_id = l.tenant_id AND v.id = l.variant_id
                      AND a.voided_at IS NULL AND a.expires_at > now() AND a.content_hash = b.root_hash)))
   OR (l.ad_id IS NOT NULL AND EXISTS (
         SELECT 1 FROM ad d WHERE d.tenant_id = l.tenant_id AND d.id = l.ad_id AND d.state IN ('active', 'paused')));
REVOKE ALL ON tracked_link_live FROM PUBLIC;
GRANT SELECT ON tracked_link_live TO cortex_redirector;
