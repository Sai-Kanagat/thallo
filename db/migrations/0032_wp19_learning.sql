-- 0032_wp19_learning.sql (WP-19): the learning loop's own tables. No shared table changes.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- regression_case  a client's eval case: a real edit (draft and human final) or a golden piece.
-- eval_run         one run of one change (prompt, model, template) on one client's case set,
--                  bound to the change hash and the case set hash.
-- blind_item       a piece in a blind comparison; its source is never shown before scoring ends.
-- blind_score      one rater's scores on one piece, four dimensions, final once written.
-- No role attributes are named, so it runs on Supabase as it does on Docker.

CREATE TABLE regression_case (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL CHECK (kind IN ('regression', 'golden')),
  edit_id uuid,
  rule_proposal_id uuid,
  scope text NOT NULL CHECK (scope IN ('pillar', 'channel', 'brand')),
  scope_ref text,
  channel text,
  language text CHECK (language IN ('en', 'it', 'ta')),
  brief jsonb NOT NULL DEFAULT '{}'::jsonb,
  draft text,
  final text NOT NULL CHECK (length(final) > 0),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, edit_id) REFERENCES edit (tenant_id, id),
  FOREIGN KEY (tenant_id, rule_proposal_id) REFERENCES rule_proposal (tenant_id, id)
);
SELECT app_enable_tenant_rls('regression_case');

CREATE TABLE eval_run (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  change_kind text NOT NULL CHECK (change_kind IN ('prompt', 'template', 'model')),
  change_name text NOT NULL CHECK (length(change_name) > 0),
  change_sha256 text NOT NULL CHECK (change_sha256 ~ '^[0-9a-f]{64}$'),
  case_set_sha256 text NOT NULL CHECK (case_set_sha256 ~ '^[0-9a-f]{64}$'),
  cases integer NOT NULL CHECK (cases >= 0),
  failed integer NOT NULL CHECK (failed >= 0),
  flagged integer NOT NULL CHECK (flagged >= 0),
  not_run integer NOT NULL CHECK (not_run >= 0),
  passed boolean NOT NULL,
  detail jsonb NOT NULL DEFAULT '[]'::jsonb,
  ran_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  CHECK (NOT passed OR (cases > 0 AND failed = 0 AND flagged = 0 AND not_run = 0))
);
CREATE INDEX eval_run_change_idx ON eval_run (tenant_id, change_sha256);
SELECT app_enable_tenant_rls('eval_run');

CREATE TABLE blind_item (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  session_id uuid NOT NULL,
  opaque_ref text NOT NULL CHECK (opaque_ref ~ '^[0-9a-f]{16}$'),
  position integer NOT NULL CHECK (position >= 0),
  source text NOT NULL CHECK (source IN ('thallo', 'agency')),
  brief_ref text NOT NULL,
  text text NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, session_id, opaque_ref),
  UNIQUE (tenant_id, session_id, position)
);
SELECT app_enable_tenant_rls('blind_item');

CREATE TABLE blind_score (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  session_id uuid NOT NULL,
  item_id uuid NOT NULL,
  rater_id uuid NOT NULL,
  brand_fit integer NOT NULL CHECK (brand_fit BETWEEN 1 AND 5),
  craft integer NOT NULL CHECK (craft BETWEEN 1 AND 5),
  specificity integer NOT NULL CHECK (specificity BETWEEN 1 AND 5),
  would_post integer NOT NULL CHECK (would_post BETWEEN 1 AND 5),
  scored_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, item_id, rater_id),
  FOREIGN KEY (tenant_id, item_id) REFERENCES blind_item (tenant_id, id)
);
SELECT app_enable_tenant_rls('blind_score');
