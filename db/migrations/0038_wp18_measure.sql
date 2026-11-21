-- 0038_wp18_measure.sql (WP-18): the decision engine's one table. No shared table changes.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- decision_proposal  one monthly proposal (keep, change, stop) with its evidence and the hash a
--                    person signs, or the month's "no decision" row. A signed row names who and when.
-- No role attributes are named, so it runs on Supabase as it does on Docker.

CREATE TABLE decision_proposal (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  period text NOT NULL CHECK (period ~ '^\d{4}-\d{2}$'),
  dimension text NOT NULL CHECK (dimension IN ('*', 'pillar', 'channel', 'format')),
  subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 120),
  kind text NOT NULL CHECK (kind IN ('keep', 'change', 'stop', 'no_decision')),
  state text NOT NULL CHECK (state IN ('proposed', 'signed', 'declined', 'noted')),
  summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND 400),
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  change jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  signed_by uuid,
  signed_at timestamptz,
  declined_by uuid,
  declined_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, period, dimension, subject),
  FOREIGN KEY (tenant_id, signed_by) REFERENCES app_user (tenant_id, id),
  FOREIGN KEY (tenant_id, declined_by) REFERENCES app_user (tenant_id, id),
  CHECK ((state = 'signed') = (signed_by IS NOT NULL AND signed_at IS NOT NULL)),
  CHECK ((state = 'declined') = (declined_by IS NOT NULL AND declined_at IS NOT NULL)),
  CHECK ((kind = 'no_decision') = (state = 'noted'))
);
SELECT app_enable_tenant_rls('decision_proposal');
