-- 0022_wp08_publisher.sql (WP-08): where each publication's send stands.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- One row per publication, same id, written in the same transaction as the publication row
-- (thallo/publish/records.py). It lets the publisher tell "in flight", "sent", "waiting for a
-- person", "not sent, retry later" and "may have landed, needs a check" apart, so a retry never
-- becomes a second post. No shared table changes.

CREATE TABLE publication_send (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  state text NOT NULL CHECK (state IN ('sending', 'sent', 'manual_pending', 'retry', 'failed', 'needs_check')),
  attempts integer NOT NULL DEFAULT 1 CHECK (attempts >= 1),
  requested_at timestamptz NOT NULL,
  text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
  media_sha256 jsonb NOT NULL DEFAULT '[]'::jsonb,
  next_try_at timestamptz,
  detail text,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, id) REFERENCES publication (tenant_id, id)
);
SELECT app_enable_tenant_rls('publication_send');

-- A send row is never removed: it is the record of what was attempted.
REVOKE DELETE, TRUNCATE ON publication_send FROM cortex_app;
