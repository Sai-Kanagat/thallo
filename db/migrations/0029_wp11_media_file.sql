-- 0029_wp11_media_file.sql (WP-11): what made each media file, what it cost and what it is labelled.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- One row per media row, written in the same transaction as the media row by thallo/media/ingest.py.
-- The provenance itself stays on `media` (core, fixed at insert by 0023); this row records the
-- connection that served the run, its mode and model, the cost, the licence of a stock file, the
-- disclosure label taken from the region policy, whether the file needs a FLAG (anything
-- generated), and the scope of the choice (a per-piece override from the review card is
-- 'piece' with the piece id). `settings` is the provider's own provenance settings (template, lint
-- verdicts, stock photographer and source URL); it never holds a secret or a vendor body.
-- Append-only: what made a file does not change. No shared table changes. No role attributes are
-- named, so it runs on Supabase as on Docker.

CREATE TABLE media_file (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  media_id uuid NOT NULL,
  connection_id uuid,
  provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 100),
  mode text NOT NULL CHECK (mode IN ('manual', 'local', 'hosted', 'mock')),
  model text,
  cost_eur numeric(12, 4) NOT NULL CHECK (cost_eur >= 0),
  licence text,
  disclosure_label text,
  flagged boolean NOT NULL DEFAULT false,
  choice_scope text NOT NULL CHECK (choice_scope IN ('tenant', 'brand', 'format', 'piece')),
  choice_ref text,
  settings jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, media_id),
  CHECK ((choice_scope = 'tenant') = (choice_ref IS NULL)),
  FOREIGN KEY (tenant_id, media_id) REFERENCES media (tenant_id, id)
);
SELECT app_enable_tenant_rls('media_file');

REVOKE UPDATE, DELETE, TRUNCATE ON media_file FROM cortex_app;
