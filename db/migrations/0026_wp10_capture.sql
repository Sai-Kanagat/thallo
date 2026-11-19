-- 0026_wp10_capture.sql (WP-10): source capture. Three weekly questions per tenant, the answers
-- people record or type, the optional long interview, and the weekly shot list.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- What a person says or types is tenant data and untrusted text. It lives on `source` (core) with
-- untrusted = true; these tables only point at it. Recordings are `media` rows (core). A recording
-- can never be stored without a consent row: capture_answer refuses a media_id without a
-- consent_id, and shot_request refuses a fulfilled shot with people in it without one.
-- No shared table changes. No role attributes are named, so it runs on Supabase as on Docker.

CREATE TABLE capture_question (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  week_start date NOT NULL CHECK (extract(isodow FROM week_start) = 1),
  position smallint NOT NULL CHECK (position BETWEEN 1 AND 3),
  play_code text,
  text text NOT NULL CHECK (length(text) BETWEEN 1 AND 500),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, week_start, position)
);
SELECT app_enable_tenant_rls('capture_question');

CREATE TABLE capture_answer (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  question_id uuid,
  mode text NOT NULL CHECK (mode IN ('question', 'interview', 'typed')),
  source_id uuid NOT NULL,
  media_id uuid,
  consent_id uuid,
  answered_by uuid NOT NULL,
  transcript_state text NOT NULL CHECK (transcript_state IN ('pending', 'done', 'needs_typing', 'failed')),
  provider text,
  detail text,
  candidates_extracted boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  CHECK (media_id IS NULL OR consent_id IS NOT NULL),
  CHECK ((mode = 'typed') = (media_id IS NULL)),
  CHECK (mode <> 'interview' OR question_id IS NULL),
  FOREIGN KEY (tenant_id, question_id) REFERENCES capture_question (tenant_id, id),
  FOREIGN KEY (tenant_id, source_id) REFERENCES source (tenant_id, id),
  FOREIGN KEY (tenant_id, media_id) REFERENCES media (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, answered_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('capture_answer');
CREATE INDEX capture_answer_question_idx ON capture_answer (tenant_id, question_id);
CREATE INDEX capture_answer_source_idx ON capture_answer (tenant_id, source_id);

CREATE TABLE shot_request (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  week_start date NOT NULL CHECK (extract(isodow FROM week_start) = 1),
  position smallint NOT NULL CHECK (position BETWEEN 1 AND 10),
  text text NOT NULL CHECK (length(text) BETWEEN 1 AND 500),
  people_in_shot boolean NOT NULL DEFAULT false,
  state text NOT NULL CHECK (state IN ('open', 'fulfilled', 'dropped')),
  media_id uuid,
  consent_id uuid,
  fulfilled_by uuid,
  fulfilled_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, week_start, position),
  CHECK (state <> 'fulfilled' OR (media_id IS NOT NULL AND fulfilled_by IS NOT NULL AND fulfilled_at IS NOT NULL)),
  CHECK (NOT people_in_shot OR media_id IS NULL OR consent_id IS NOT NULL),
  FOREIGN KEY (tenant_id, media_id) REFERENCES media (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, fulfilled_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('shot_request');

-- Answers and shot requests are the record of what people gave; they are never removed.
REVOKE DELETE, TRUNCATE ON capture_answer FROM cortex_app;
REVOKE DELETE, TRUNCATE ON shot_request FROM cortex_app;
