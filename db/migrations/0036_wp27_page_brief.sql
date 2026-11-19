-- 0036_wp27_page_brief.sql (WP-27): one answer-first page brief per real query cluster.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- A row records that a Search Console query cluster with impressions and no covering page became
-- a page brief (thallo/extensions/pages). The page itself lives in the normal content tables
-- (idea, brief, asset, bundle, variant on channel 'website'), so the gate and a human approve it
-- like any other piece; this table only adds what the gate does not know: the cluster, the queries
-- (untrusted text from the export, stored as data), the named human reviewer and the slug.
--
-- Guards against thin pages at scale: one row per cluster key per tenant (unique), a named
-- reviewer on every row (NOT NULL), and no update or delete for the app role, so a cluster cannot
-- be recycled into a second page by rewriting its key. No role attributes are named, so it runs on
-- Supabase as it does on Docker.

CREATE TABLE page_brief (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  cluster_key text NOT NULL CHECK (cluster_key ~ '^[a-z0-9 ]{1,200}$'),
  slug text NOT NULL CHECK (slug ~ '^/[a-z]+(-[a-z]+)*(/[a-z]+(-[a-z]+)*)*$'),
  queries jsonb NOT NULL DEFAULT '[]'::jsonb,
  impressions integer NOT NULL CHECK (impressions >= 0),
  reviewer_id uuid NOT NULL,
  variant_id uuid NOT NULL,
  status text NOT NULL CHECK (status IN ('checking', 'needs_human')),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, cluster_key),
  UNIQUE (tenant_id, slug),
  FOREIGN KEY (tenant_id, reviewer_id) REFERENCES app_user (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id)
);
SELECT app_enable_tenant_rls('page_brief');

REVOKE UPDATE, DELETE, TRUNCATE ON page_brief FROM cortex_app;
