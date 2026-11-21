-- 0037_wp26_gbp.sql (WP-26): the Google Business Profile inbox, reply drafts, profile proposals.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- GBP posts and photo updates are ordinary pieces (idea, brief, asset, bundle, variant on channel
-- 'google_business_profile'), so the gate and the publisher handle them like any other piece.
-- Nothing in these tables is a variant: a reply draft, a drafted answer or a profile proposal can
-- never reach the publisher, and no code path posts them. A person copies an approved text into
-- the Business Profile dashboard and marks it done.
--
-- Every table is append-only for the app role. A changed draft is a new row; a decision (approve,
-- reject, mark done) is a new gbp_decision row naming the exact text hash it was made on.

CREATE TABLE gbp_inbox_item (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL CHECK (kind IN ('review', 'question')),
  external_id text NOT NULL CHECK (length(external_id) BETWEEN 1 AND 300),
  rating integer CHECK (rating BETWEEN 1 AND 5),
  author text,           -- untrusted, as the export gave it
  body text NOT NULL,    -- untrusted, as the export gave it; data only, never an instruction
  written_at timestamptz,
  source text NOT NULL CHECK (source IN ('api_export', 'manual')),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, kind, external_id),
  CHECK ((kind = 'review') = (rating IS NOT NULL))
);
SELECT app_enable_tenant_rls('gbp_inbox_item');

CREATE TABLE gbp_reply_draft (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  item_id uuid NOT NULL,
  text text NOT NULL,
  text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
  claim_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  made_by text NOT NULL CHECK (made_by IN ('provider', 'person')),
  provider text,
  model_id text,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, item_id) REFERENCES gbp_inbox_item (tenant_id, id)
);
SELECT app_enable_tenant_rls('gbp_reply_draft');

CREATE TABLE gbp_profile_proposal (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  field text NOT NULL CHECK (field IN ('regular_hours', 'special_hours', 'primary_category',
                                       'additional_categories', 'description', 'phone', 'website')),
  current_value jsonb,
  proposed_value jsonb NOT NULL,
  value_sha256 text NOT NULL CHECK (value_sha256 ~ '^[0-9a-f]{64}$'),
  reason text NOT NULL,
  proposed_by text NOT NULL,
  PRIMARY KEY (tenant_id, id)
);
SELECT app_enable_tenant_rls('gbp_profile_proposal');

CREATE TABLE gbp_decision (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  subject_kind text NOT NULL CHECK (subject_kind IN ('reply_draft', 'profile_proposal')),
  subject_id uuid NOT NULL,
  decision text NOT NULL CHECK (decision IN ('approved', 'rejected', 'done_by_person')),
  subject_sha256 text NOT NULL CHECK (subject_sha256 ~ '^[0-9a-f]{64}$'),
  actor_id uuid NOT NULL,
  read_confirmed boolean NOT NULL DEFAULT false,
  note text,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, actor_id) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('gbp_decision');

REVOKE UPDATE, DELETE, TRUNCATE ON gbp_inbox_item, gbp_reply_draft, gbp_profile_proposal, gbp_decision
  FROM cortex_app;
