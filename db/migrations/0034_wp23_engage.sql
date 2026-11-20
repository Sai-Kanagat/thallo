-- 0034_wp23_engage.sql (WP-23): the engagement inbox's one table. No shared table changes.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- engage_item  one comment or message from a channel, with its triage and a suggested reply.
--              author and text are untrusted data. There is no send column and no send table:
--              a person replies in the platform and marks the item handled.
--              (tenant_id, channel, external_id) is unique, so a feed delivering twice stores once.
--              A flagged item (abuse, crisis) carries no suggestion until a person acknowledged it.
-- No role attributes are named, so it runs on Supabase as it does on Docker.

CREATE TABLE engage_item (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  channel text NOT NULL CHECK (channel ~ '^[a-z_]{1,40}$'),
  source text NOT NULL CHECK (source IN ('comment', 'message')),
  external_id text NOT NULL CHECK (length(external_id) BETWEEN 1 AND 200),
  author text CHECK (author IS NULL OR length(author) <= 80),
  text text NOT NULL CHECK (length(text) <= 4000),
  permalink text CHECK (permalink IS NULL OR permalink ~ '^https://'),
  received_at timestamptz NOT NULL,
  category text NOT NULL CHECK (category IN ('question', 'praise', 'complaint', 'lead', 'abuse', 'crisis',
                                             'spam', 'other')),
  reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
  state text NOT NULL CHECK (state IN ('flagged', 'open', 'handled', 'handed_to_leads', 'dismissed')),
  suggested_reply text CHECK (suggested_reply IS NULL OR length(suggested_reply) <= 1500),
  suggestion_model text,
  prompt_version text,
  acknowledged_by uuid,
  handled_by uuid,
  lead_id uuid,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, channel, external_id),
  CHECK (state <> 'flagged' OR suggested_reply IS NULL),
  CHECK (category NOT IN ('abuse', 'crisis') OR state IN ('flagged', 'dismissed') OR acknowledged_by IS NOT NULL),
  CHECK (state <> 'handed_to_leads' OR lead_id IS NOT NULL),
  FOREIGN KEY (tenant_id, acknowledged_by) REFERENCES app_user (tenant_id, id),
  FOREIGN KEY (tenant_id, handled_by) REFERENCES app_user (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES lead (tenant_id, id)
);
CREATE INDEX engage_item_state_idx ON engage_item (tenant_id, state);
SELECT app_enable_tenant_rls('engage_item');
REVOKE DELETE, TRUNCATE ON engage_item FROM cortex_app;
