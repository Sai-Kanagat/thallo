-- 0031_wp14_nurture_crm.sql (WP-14): nurture sequences and the CRM sync ledger. No shared table changes.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- nurture_sequence    hash-approved nurture templates in order, the waits, quiet hours and caps.
-- nurture_enrolment   one warm lead in one sequence. At most one active enrolment per lead.
-- nurture_step_draft  the message drafted for one enrolment step; the unique pair makes the
--                     runner idempotent. Every message still waits for a person's tap.
-- crm_sync_record     the last projection hash and CRM id pushed per person and provider.
-- No role attributes are named, so it runs on Supabase as it does on Docker.

CREATE TABLE nurture_sequence (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  channel text NOT NULL CHECK (channel IN ('whatsapp', 'email', 'sms')),
  steps jsonb NOT NULL DEFAULT '[]'::jsonb,
  quiet_start_hour integer NOT NULL DEFAULT 20 CHECK (quiet_start_hour BETWEEN 0 AND 23),
  quiet_end_hour integer NOT NULL DEFAULT 9 CHECK (quiet_end_hour BETWEEN 0 AND 23),
  max_per_week integer NOT NULL DEFAULT 2 CHECK (max_per_week BETWEEN 1 AND 7),
  min_gap_hours integer NOT NULL DEFAULT 48 CHECK (min_gap_hours BETWEEN 1 AND 720),
  active boolean NOT NULL DEFAULT true,
  PRIMARY KEY (tenant_id, id)
);
SELECT app_enable_tenant_rls('nurture_sequence');

CREATE TABLE nurture_enrolment (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  lead_id uuid NOT NULL,
  person_id uuid NOT NULL,
  sequence_id uuid NOT NULL,
  state text NOT NULL CHECK (state IN ('active', 'stopped', 'done')),
  step integer NOT NULL DEFAULT 0 CHECK (step >= 0),
  due_at timestamptz NOT NULL,
  last_touch_id uuid,
  stop_reason text,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES lead (tenant_id, id),
  FOREIGN KEY (tenant_id, person_id) REFERENCES person (tenant_id, id),
  FOREIGN KEY (tenant_id, sequence_id) REFERENCES nurture_sequence (tenant_id, id)
);
CREATE UNIQUE INDEX nurture_enrolment_one_active ON nurture_enrolment (tenant_id, lead_id) WHERE state = 'active';
SELECT app_enable_tenant_rls('nurture_enrolment');

CREATE TABLE nurture_step_draft (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  enrolment_id uuid NOT NULL,
  step integer NOT NULL CHECK (step >= 0),
  message_id uuid NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, enrolment_id, step),
  UNIQUE (tenant_id, message_id),
  FOREIGN KEY (tenant_id, enrolment_id) REFERENCES nurture_enrolment (tenant_id, id),
  FOREIGN KEY (tenant_id, message_id) REFERENCES message (tenant_id, id)
);
SELECT app_enable_tenant_rls('nurture_step_draft');

CREATE TABLE crm_sync_record (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  provider text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_]{0,39}$'),
  person_id uuid NOT NULL,
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  external_id text,
  erased boolean NOT NULL DEFAULT false,
  synced_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, provider, person_id),
  FOREIGN KEY (tenant_id, person_id) REFERENCES person (tenant_id, id)
);
SELECT app_enable_tenant_rls('crm_sync_record');
