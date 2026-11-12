-- 0001_core.sql: the frozen core schema (WP-00).
-- GENERATED from cortex_operator/contracts/models.py. Do not edit by hand.
-- Regenerate: python -m cortex_operator.contracts.ddl > db/migrations/0001_core.sql
-- Row-level security is added by WP-01 in a later migration.
-- Every foreign key between tenant tables is composite (tenant_id, x_id), so no row can
-- point at another tenant's row. scripts/migrate.py wraps each file in one transaction.

CREATE TABLE operator (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  name text NOT NULL
);

CREATE TABLE identity (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  email text NOT NULL UNIQUE,
  display_name text NOT NULL
);

CREATE TABLE tenant (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  operator_id uuid NOT NULL REFERENCES operator(id),
  name text NOT NULL,
  slug text NOT NULL UNIQUE,
  default_language text NOT NULL CHECK (default_language IN ('en', 'it', 'ta')),
  region_code text NOT NULL,
  currency text NOT NULL,
  timezone text NOT NULL DEFAULT 'UTC',
  paused boolean NOT NULL DEFAULT false
);
CREATE INDEX tenant_operator_id_idx ON tenant (operator_id);

CREATE TABLE app_user (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  identity_id uuid NOT NULL REFERENCES identity(id),
  display_name text NOT NULL,
  role text NOT NULL CHECK (role IN ('operator', 'founder', 'approver', 'seller', 'staff')),
  language text CHECK (language IN ('en', 'it', 'ta')),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, identity_id)
);
CREATE INDEX app_user_tenant_idx ON app_user (tenant_id);
CREATE INDEX app_user_identity_id_idx ON app_user (identity_id);

CREATE TABLE region_policy (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  region_code text NOT NULL,
  version bigint NOT NULL,
  rules jsonb NOT NULL DEFAULT '{}'::jsonb,
  disclosure_label text,
  effective_from timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, region_code, version)
);
CREATE INDEX region_policy_tenant_idx ON region_policy (tenant_id);

CREATE TABLE connection (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  capability text NOT NULL CHECK (capability IN ('text', 'image', 'video', 'transcription', 'voiceover', 'publishing', 'calendar', 'crm', 'messaging', 'booking', 'analytics', 'ads', 'research', 'storage', 'notifications', 'automation_hooks')),
  provider text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('manual', 'local', 'hosted', 'mock')),
  model text,
  role text CHECK (role IN ('plan', 'brief', 'draft', 'extract', 'classify', 'grade', 'second_read', 'memo')),
  scope text NOT NULL DEFAULT 'tenant',
  scope_ref text,
  priority bigint NOT NULL DEFAULT 0,
  enabled boolean NOT NULL DEFAULT true,
  settings jsonb NOT NULL DEFAULT '{}'::jsonb,
  secret_ref text,
  UNIQUE (tenant_id, id)
);
CREATE INDEX connection_tenant_idx ON connection (tenant_id);

CREATE TABLE channel_account (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  channel text NOT NULL CHECK (channel IN ('linkedin_personal', 'linkedin_company', 'facebook', 'instagram', 'google_business_profile', 'whatsapp', 'email', 'newsletter', 'website', 'youtube', 'search_ads', 'social_ads')),
  connection_id uuid,
  handle text NOT NULL,
  token_expires_at timestamptz,
  secret_ref text,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connection (tenant_id, id)
);
CREATE INDEX channel_account_tenant_idx ON channel_account (tenant_id);
CREATE INDEX channel_account_connection_id_idx ON channel_account (connection_id);

CREATE TABLE play (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  code text NOT NULL,
  name text NOT NULL,
  channels jsonb NOT NULL DEFAULT '[]'::jsonb,
  formats jsonb NOT NULL DEFAULT '[]'::jsonb,
  qualification_questions jsonb NOT NULL DEFAULT '[]'::jsonb,
  booked_definition text NOT NULL,
  pillar_mix jsonb NOT NULL DEFAULT '{}'::jsonb,
  metrics jsonb NOT NULL DEFAULT '[]'::jsonb,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, code)
);
CREATE INDEX play_tenant_idx ON play (tenant_id);

CREATE TABLE growth_model (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  version bigint NOT NULL,
  play_id uuid,
  stages jsonb NOT NULL DEFAULT '[]'::jsonb,
  bottleneck text,
  margin_per_won numeric(14,4),
  share_of_margin numeric(14,4) NOT NULL DEFAULT 0.30,
  allowable_cost_eur numeric(14,4),
  active boolean NOT NULL DEFAULT false,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, version),
  FOREIGN KEY (tenant_id, play_id) REFERENCES play (tenant_id, id)
);
CREATE INDEX growth_model_tenant_idx ON growth_model (tenant_id);
CREATE INDEX growth_model_play_id_idx ON growth_model (play_id);

CREATE TABLE pillar (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  growth_model_id uuid NOT NULL,
  name text NOT NULL,
  stage text NOT NULL CHECK (stage IN ('awareness', 'consideration', 'conversion', 'retention')),
  target_share numeric(14,4) NOT NULL,
  min_share numeric(14,4) NOT NULL DEFAULT 0.10,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, growth_model_id) REFERENCES growth_model (tenant_id, id)
);
CREATE INDEX pillar_tenant_idx ON pillar (tenant_id);
CREATE INDEX pillar_growth_model_id_idx ON pillar (growth_model_id);

CREATE TABLE account (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  name text NOT NULL,
  domain text,
  UNIQUE (tenant_id, id)
);
CREATE INDEX account_tenant_idx ON account (tenant_id);

CREATE TABLE person (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  account_id uuid,
  display_name text,
  phone text,
  email text,
  language text CHECK (language IN ('en', 'it', 'ta')),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, account_id) REFERENCES account (tenant_id, id)
);
CREATE INDEX person_tenant_idx ON person (tenant_id);
CREATE INDEX person_account_id_idx ON person (account_id);

CREATE TABLE consent (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  person_id uuid,
  subject_label text,
  basis text NOT NULL CHECK (basis IN ('opt_in', 'enquiry', 'existing_customer', 'none')),
  channel text CHECK (channel IN ('whatsapp', 'email', 'sms')),
  scope text NOT NULL CHECK (scope IN ('contact', 'likeness', 'voice', 'quote')),
  evidence text NOT NULL,
  given_at timestamptz NOT NULL,
  withdrawn_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, person_id) REFERENCES person (tenant_id, id)
);
CREATE INDEX consent_tenant_idx ON consent (tenant_id);
CREATE INDEX consent_person_id_idx ON consent (person_id);

CREATE TABLE media (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL CHECK (kind IN ('image', 'video', 'audio', 'pdf')),
  sha256 text NOT NULL,
  mime text NOT NULL,
  size_bytes bigint NOT NULL,
  storage_uri text NOT NULL,
  provenance text NOT NULL CHECK (provenance IN ('client_photo', 'client_clip', 'client_graphic', 'template_render', 'generated_background', 'generated', 'person_recording', 'person_file')),
  connection_id uuid,
  generation_prompt text,
  consent_id uuid,
  alt_text text,
  disclosure_label text,
  width bigint,
  height bigint,
  duration_ms bigint,
  has_voice_track boolean NOT NULL DEFAULT false,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, sha256),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connection (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id)
);
CREATE INDEX media_tenant_idx ON media (tenant_id);
CREATE INDEX media_connection_id_idx ON media (connection_id);
CREATE INDEX media_consent_id_idx ON media (consent_id);

CREATE TABLE source (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL CHECK (kind IN ('voice_answer', 'interview', 'document', 'web_page', 'seller_note', 'typed', 'research')),
  language text CHECK (language IN ('en', 'it', 'ta')),
  title text NOT NULL,
  transcript text,
  uri text,
  media_id uuid,
  consent_id uuid,
  untrusted boolean NOT NULL DEFAULT false,
  captured_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, media_id) REFERENCES media (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id)
);
CREATE INDEX source_tenant_idx ON source (tenant_id);
CREATE INDEX source_media_id_idx ON source (media_id);
CREATE INDEX source_consent_id_idx ON source (consent_id);

CREATE TABLE claim (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  code text NOT NULL,
  version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
  supersedes_id uuid,
  kind text NOT NULL CHECK (kind IN ('number', 'price', 'date', 'name', 'quote', 'capability', 'offer')),
  state text NOT NULL DEFAULT 'candidate' CHECK (state IN ('candidate', 'verified', 'expired', 'voided', 'rejected')),
  value text NOT NULL,
  unit text,
  as_of date,
  source_id uuid,
  source_span jsonb,
  evidence_sha256 text,
  verified_by uuid,
  verified_at timestamptz,
  expires_at timestamptz,
  expiry_reason text,
  languages jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, code, version),
  FOREIGN KEY (tenant_id, supersedes_id) REFERENCES claim (tenant_id, id),
  FOREIGN KEY (tenant_id, source_id) REFERENCES source (tenant_id, id),
  FOREIGN KEY (tenant_id, verified_by) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX claim_tenant_idx ON claim (tenant_id);
CREATE INDEX claim_supersedes_id_idx ON claim (supersedes_id);
CREATE INDEX claim_source_id_idx ON claim (source_id);
CREATE INDEX claim_verified_by_idx ON claim (verified_by);

CREATE TABLE signal (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL,
  channel text CHECK (channel IN ('linkedin_personal', 'linkedin_company', 'facebook', 'instagram', 'google_business_profile', 'whatsapp', 'email', 'newsletter', 'website', 'youtube', 'search_ads', 'social_ads')),
  provider text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  observed_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id)
);
CREATE INDEX signal_tenant_idx ON signal (tenant_id);

CREATE TABLE idea (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  pillar_id uuid,
  state text NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed', 'accepted', 'briefed', 'killed')),
  stage text NOT NULL CHECK (stage IN ('awareness', 'consideration', 'conversion', 'retention')),
  title text NOT NULL,
  rationale text NOT NULL,
  source_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  is_experiment boolean NOT NULL DEFAULT false,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, pillar_id) REFERENCES pillar (tenant_id, id)
);
CREATE INDEX idea_tenant_idx ON idea (tenant_id);
CREATE INDEX idea_pillar_id_idx ON idea (pillar_id);

CREATE TABLE idea_signal (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  idea_id uuid NOT NULL,
  signal_id uuid NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (idea_id, signal_id),
  FOREIGN KEY (tenant_id, idea_id) REFERENCES idea (tenant_id, id),
  FOREIGN KEY (tenant_id, signal_id) REFERENCES signal (tenant_id, id)
);
CREATE INDEX idea_signal_tenant_idx ON idea_signal (tenant_id);
CREATE INDEX idea_signal_idea_id_idx ON idea_signal (idea_id);
CREATE INDEX idea_signal_signal_id_idx ON idea_signal (signal_id);

CREATE TABLE brief (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  idea_id uuid NOT NULL UNIQUE,
  audience text NOT NULL,
  job_to_be_done text NOT NULL,
  message text NOT NULL,
  channels jsonb NOT NULL DEFAULT '[]'::jsonb,
  allowed_claim_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  call_to_action text NOT NULL,
  language text NOT NULL CHECK (language IN ('en', 'it', 'ta')),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, idea_id) REFERENCES idea (tenant_id, id)
);
CREATE INDEX brief_tenant_idx ON brief (tenant_id);
CREATE INDEX brief_idea_id_idx ON brief (idea_id);

CREATE TABLE forecast (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  brief_id uuid NOT NULL UNIQUE,
  seq bigint NOT NULL,
  metric text NOT NULL,
  day smallint NOT NULL CHECK (day IN (2, 7, 14)),
  low double precision NOT NULL,
  high double precision NOT NULL,
  baseline_low double precision NOT NULL,
  baseline_high double precision NOT NULL,
  sealed_at timestamptz NOT NULL,
  prev_hash text,
  seal_hash text NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, seq),
  FOREIGN KEY (tenant_id, brief_id) REFERENCES brief (tenant_id, id)
);
CREATE INDEX forecast_tenant_idx ON forecast (tenant_id);
CREATE INDEX forecast_brief_id_idx ON forecast (brief_id);

CREATE TABLE forecast_score (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  forecast_id uuid NOT NULL UNIQUE,
  actual double precision,
  interval_score double precision,
  baseline_score double precision,
  beat_baseline boolean,
  scored_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, forecast_id) REFERENCES forecast (tenant_id, id)
);
CREATE INDEX forecast_score_tenant_idx ON forecast_score (tenant_id);
CREATE INDEX forecast_score_forecast_id_idx ON forecast_score (forecast_id);

CREATE TABLE asset (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  brief_id uuid NOT NULL,
  master_text text NOT NULL,
  candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
  selection_reason text,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, brief_id) REFERENCES brief (tenant_id, id)
);
CREATE INDEX asset_tenant_idx ON asset (tenant_id);
CREATE INDEX asset_brief_id_idx ON asset (brief_id);

CREATE TABLE bundle (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  asset_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'in_review', 'approved', 'voided', 'expired', 'rejected')),
  root_hash text,
  rule_version bigint NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES asset (tenant_id, id)
);
CREATE INDEX bundle_tenant_idx ON bundle (tenant_id);
CREATE INDEX bundle_asset_id_idx ON bundle (asset_id);

CREATE TABLE variant (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  bundle_id uuid NOT NULL,
  channel text NOT NULL CHECK (channel IN ('linkedin_personal', 'linkedin_company', 'facebook', 'instagram', 'google_business_profile', 'whatsapp', 'email', 'newsletter', 'website', 'youtube', 'search_ads', 'social_ads')),
  language text NOT NULL CHECK (language IN ('en', 'it', 'ta')),
  state text NOT NULL DEFAULT 'drafting' CHECK (state IN ('drafting', 'needs_evidence', 'checking', 'review', 'on_hold', 'approved', 'benched', 'scheduled', 'published', 'read_back_ok', 'read_back_mismatch', 'rejected', 'failed')),
  version bigint NOT NULL DEFAULT 1,
  text text NOT NULL,
  rendered_text text,
  template_ref text,
  content_sha256 text,
  scheduled_for timestamptz,
  task_type text NOT NULL,
  is_canary boolean NOT NULL DEFAULT false,
  canary_flaw text,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, bundle_id) REFERENCES bundle (tenant_id, id)
);
CREATE INDEX variant_tenant_idx ON variant (tenant_id);
CREATE INDEX variant_bundle_id_idx ON variant (bundle_id);

CREATE TABLE variant_claim (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  variant_id uuid NOT NULL,
  claim_id uuid NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (variant_id, claim_id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, claim_id) REFERENCES claim (tenant_id, id)
);
CREATE INDEX variant_claim_tenant_idx ON variant_claim (tenant_id);
CREATE INDEX variant_claim_variant_id_idx ON variant_claim (variant_id);
CREATE INDEX variant_claim_claim_id_idx ON variant_claim (claim_id);

CREATE TABLE variant_media (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  variant_id uuid NOT NULL,
  media_id uuid NOT NULL,
  position bigint NOT NULL DEFAULT 0,
  UNIQUE (tenant_id, id),
  UNIQUE (variant_id, media_id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, media_id) REFERENCES media (tenant_id, id)
);
CREATE INDEX variant_media_tenant_idx ON variant_media (tenant_id);
CREATE INDEX variant_media_variant_id_idx ON variant_media (variant_id);
CREATE INDEX variant_media_media_id_idx ON variant_media (media_id);

CREATE TABLE check_result (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  variant_id uuid NOT NULL,
  layer text NOT NULL CHECK (layer IN ('code', 'grader', 'second_reader')),
  check_name text NOT NULL,
  verdict text NOT NULL CHECK (verdict IN ('pass', 'flag', 'fail')),
  reason text,
  attempt bigint NOT NULL DEFAULT 1,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id)
);
CREATE INDEX check_result_tenant_idx ON check_result (tenant_id);
CREATE INDEX check_result_variant_id_idx ON check_result (variant_id);

CREATE TABLE plan_slot (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  week_start date NOT NULL,
  channel text NOT NULL CHECK (channel IN ('linkedin_personal', 'linkedin_company', 'facebook', 'instagram', 'google_business_profile', 'whatsapp', 'email', 'newsletter', 'website', 'youtube', 'search_ads', 'social_ads')),
  pillar_id uuid,
  stage text NOT NULL CHECK (stage IN ('awareness', 'consideration', 'conversion', 'retention')),
  starts_at timestamptz NOT NULL,
  reserved_for_experiment boolean NOT NULL DEFAULT false,
  variant_id uuid,
  state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'filled', 'benched', 'published', 'missed', 'killed')),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, pillar_id) REFERENCES pillar (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id)
);
CREATE INDEX plan_slot_tenant_idx ON plan_slot (tenant_id);
CREATE INDEX plan_slot_pillar_id_idx ON plan_slot (pillar_id);
CREATE INDEX plan_slot_variant_id_idx ON plan_slot (variant_id);

CREATE TABLE approval (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  subject_kind text NOT NULL CHECK (subject_kind IN ('bundle', 'spend_proposal', 'template', 'trust_level', 'rule_proposal')),
  subject_id uuid NOT NULL,
  content_hash text NOT NULL,
  approver_id uuid NOT NULL,
  method text NOT NULL CHECK (method IN ('per_bundle', 'express_list', 'template_instance')),
  seconds_on_screen double precision,
  approved_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  canary boolean NOT NULL DEFAULT false,
  voided_at timestamptz,
  void_reason text,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, approver_id) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX approval_tenant_idx ON approval (tenant_id);
CREATE INDEX approval_subject_id_idx ON approval (subject_id);
CREATE INDEX approval_approver_id_idx ON approval (approver_id);

CREATE TABLE publication (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  variant_id uuid NOT NULL,
  approval_id uuid NOT NULL,
  connection_id uuid,
  idempotency_key text NOT NULL,
  content_sha256 text NOT NULL,
  external_id text,
  live_url text,
  sent_at timestamptz,
  readback_at timestamptz,
  readback_ok boolean,
  readback_diff text,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, variant_id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, approval_id) REFERENCES approval (tenant_id, id),
  FOREIGN KEY (tenant_id, connection_id) REFERENCES connection (tenant_id, id)
);
CREATE INDEX publication_tenant_idx ON publication (tenant_id);
CREATE INDEX publication_variant_id_idx ON publication (variant_id);
CREATE INDEX publication_approval_id_idx ON publication (approval_id);
CREATE INDEX publication_connection_id_idx ON publication (connection_id);

CREATE TABLE metric_snapshot (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  publication_id uuid NOT NULL,
  day smallint NOT NULL CHECK (day IN (2, 7, 14)),
  metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  missing jsonb NOT NULL DEFAULT '[]'::jsonb,
  pulled_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (publication_id, day),
  FOREIGN KEY (tenant_id, publication_id) REFERENCES publication (tenant_id, id)
);
CREATE INDEX metric_snapshot_tenant_idx ON metric_snapshot (tenant_id);
CREATE INDEX metric_snapshot_publication_id_idx ON metric_snapshot (publication_id);

CREATE TABLE edit (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  variant_id uuid NOT NULL,
  editor_id uuid NOT NULL,
  before_text text NOT NULL,
  after_text text NOT NULL,
  kind text CHECK (kind IN ('word_swap', 'cut', 'tone_shift', 'structure_change', 'fact_fix', 'typo')),
  reason text,
  rejection_reason text CHECK (rejection_reason IN ('off_voice', 'generic', 'wrong_fact', 'weak_visual', 'wrong_timing', 'not_interesting', 'other')),
  counts_as_defect boolean NOT NULL DEFAULT false,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, editor_id) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX edit_tenant_idx ON edit (tenant_id);
CREATE INDEX edit_variant_id_idx ON edit (variant_id);
CREATE INDEX edit_editor_id_idx ON edit (editor_id);

CREATE TABLE rule_proposal (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  state text NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed', 'accepted', 'promoted', 'rejected', 'expired')),
  scope text NOT NULL CHECK (scope IN ('pillar', 'channel', 'brand')),
  scope_ref text,
  rule jsonb NOT NULL,
  triggering_edit_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  regression_case jsonb NOT NULL,
  expires_at timestamptz,
  UNIQUE (tenant_id, id)
);
CREATE INDEX rule_proposal_tenant_idx ON rule_proposal (tenant_id);

CREATE TABLE voice_rule (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  rule_proposal_id uuid,
  scope text NOT NULL CHECK (scope IN ('pillar', 'channel', 'brand')),
  scope_ref text,
  language text CHECK (language IN ('en', 'it', 'ta')),
  body jsonb NOT NULL,
  last_used_at timestamptz,
  expires_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, rule_proposal_id) REFERENCES rule_proposal (tenant_id, id)
);
CREATE INDEX voice_rule_tenant_idx ON voice_rule (tenant_id);
CREATE INDEX voice_rule_rule_proposal_id_idx ON voice_rule (rule_proposal_id);

CREATE TABLE campaign (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  platform text NOT NULL,
  external_id text,
  name text NOT NULL,
  role text NOT NULL CHECK (role IN ('brand_search', 'high_intent_search', 'local_social', 'boost', 'retargeting', 'account_list')),
  state text NOT NULL DEFAULT 'created_paused' CHECK (state IN ('created_paused', 'active', 'paused', 'archived')),
  daily_budget numeric(14,4),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, platform, external_id)
);
CREATE INDEX campaign_tenant_idx ON campaign (tenant_id);

CREATE TABLE ad (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  campaign_id uuid NOT NULL,
  variant_id uuid,
  platform text NOT NULL,
  external_id text,
  state text NOT NULL DEFAULT 'created_paused' CHECK (state IN ('created_paused', 'active', 'paused', 'archived')),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES campaign (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id)
);
CREATE INDEX ad_tenant_idx ON ad (tenant_id);
CREATE INDEX ad_campaign_id_idx ON ad (campaign_id);
CREATE INDEX ad_variant_id_idx ON ad (variant_id);

CREATE TABLE tracked_link (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  variant_id uuid,
  ad_id uuid,
  slug text NOT NULL UNIQUE,
  destination text NOT NULL,
  whatsapp_code text UNIQUE,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, ad_id) REFERENCES ad (tenant_id, id)
);
CREATE INDEX tracked_link_tenant_idx ON tracked_link (tenant_id);
CREATE INDEX tracked_link_variant_id_idx ON tracked_link (variant_id);
CREATE INDEX tracked_link_ad_id_idx ON tracked_link (ad_id);

CREATE TABLE touch (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL CHECK (kind IN ('link', 'whatsapp_code', 'form', 'call', 'walk_in_code', 'self_report')),
  person_id uuid,
  variant_id uuid,
  ad_id uuid,
  tracked_link_id uuid,
  code text,
  self_report text,
  occurred_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, person_id) REFERENCES person (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, ad_id) REFERENCES ad (tenant_id, id),
  FOREIGN KEY (tenant_id, tracked_link_id) REFERENCES tracked_link (tenant_id, id)
);
CREATE INDEX touch_tenant_idx ON touch (tenant_id);
CREATE INDEX touch_person_id_idx ON touch (person_id);
CREATE INDEX touch_variant_id_idx ON touch (variant_id);
CREATE INDEX touch_ad_id_idx ON touch (ad_id);
CREATE INDEX touch_tracked_link_id_idx ON touch (tracked_link_id);

CREATE TABLE lead (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  person_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'new' CHECK (state IN ('new', 'acknowledged', 'qualifying', 'routed', 'nurturing', 'contacted', 'booked', 'held', 'no_show', 'won', 'lost', 'parked')),
  route text CHECK (route IN ('hot', 'warm', 'low_fit')),
  fit bigint NOT NULL DEFAULT 0 CHECK (fit >= 0 AND fit <= 100),
  heat double precision NOT NULL DEFAULT 0.0,
  heat_updated_at timestamptz,
  answers jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_touch_id uuid,
  last_touch_id uuid,
  owner_id uuid,
  clock_started_at timestamptz,
  first_response_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, person_id) REFERENCES person (tenant_id, id),
  FOREIGN KEY (tenant_id, first_touch_id) REFERENCES touch (tenant_id, id),
  FOREIGN KEY (tenant_id, last_touch_id) REFERENCES touch (tenant_id, id),
  FOREIGN KEY (tenant_id, owner_id) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX lead_tenant_idx ON lead (tenant_id);
CREATE INDEX lead_person_id_idx ON lead (person_id);
CREATE INDEX lead_first_touch_id_idx ON lead (first_touch_id);
CREATE INDEX lead_last_touch_id_idx ON lead (last_touch_id);
CREATE INDEX lead_owner_id_idx ON lead (owner_id);

CREATE TABLE message_template (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  name text NOT NULL,
  version bigint NOT NULL,
  kind text NOT NULL CHECK (kind IN ('acknowledgement', 'qualification', 'nurture', 'reminder', 'reply_draft')),
  channel text NOT NULL CHECK (channel IN ('whatsapp', 'email', 'sms')),
  language text NOT NULL CHECK (language IN ('en', 'it', 'ta')),
  body text NOT NULL,
  slot_sources jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_sha256 text NOT NULL,
  active boolean NOT NULL DEFAULT false,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name, version)
);
CREATE INDEX message_template_tenant_idx ON message_template (tenant_id);

CREATE TABLE message (
  model_id text,
  prompt_version text,
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  person_id uuid NOT NULL,
  lead_id uuid,
  kind text NOT NULL CHECK (kind IN ('acknowledgement', 'qualification', 'nurture', 'reminder', 'reply_draft')),
  channel text NOT NULL CHECK (channel IN ('whatsapp', 'email', 'sms')),
  state text NOT NULL DEFAULT 'drafted' CHECK (state IN ('drafted', 'queued', 'sent', 'delivered', 'failed', 'discarded')),
  body text NOT NULL,
  template_id uuid,
  template_approval_id uuid,
  sent_by_kind text CHECK (sent_by_kind IN ('human', 'system', 'agent')),
  sent_by_user_id uuid,
  sent_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, person_id) REFERENCES person (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES lead (tenant_id, id),
  FOREIGN KEY (tenant_id, template_id) REFERENCES message_template (tenant_id, id),
  FOREIGN KEY (tenant_id, template_approval_id) REFERENCES approval (tenant_id, id),
  FOREIGN KEY (tenant_id, sent_by_user_id) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX message_tenant_idx ON message (tenant_id);
CREATE INDEX message_person_id_idx ON message (person_id);
CREATE INDEX message_lead_id_idx ON message (lead_id);
CREATE INDEX message_template_id_idx ON message (template_id);
CREATE INDEX message_template_approval_id_idx ON message (template_approval_id);
CREATE INDEX message_sent_by_user_id_idx ON message (sent_by_user_id);

CREATE TABLE booking (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  lead_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'booked' CHECK (state IN ('booked', 'held', 'no_show', 'cancelled')),
  kind text NOT NULL,
  starts_at timestamptz NOT NULL,
  provider_ref text,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES lead (tenant_id, id)
);
CREATE INDEX booking_tenant_idx ON booking (tenant_id);
CREATE INDEX booking_lead_id_idx ON booking (lead_id);

CREATE TABLE deal (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  booking_id uuid UNIQUE,
  lead_id uuid NOT NULL,
  value numeric(14,4),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, booking_id) REFERENCES booking (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES lead (tenant_id, id)
);
CREATE INDEX deal_tenant_idx ON deal (tenant_id);
CREATE INDEX deal_booking_id_idx ON deal (booking_id);
CREATE INDEX deal_lead_id_idx ON deal (lead_id);

CREATE TABLE outcome (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  deal_id uuid NOT NULL UNIQUE,
  kind text NOT NULL CHECK (kind IN ('won', 'lost')),
  amount numeric(14,4),
  loss_reason text CHECK (loss_reason IN ('price', 'finance_refused', 'chose_competitor', 'no_budget', 'timing', 'no_response', 'not_a_fit', 'other')),
  recorded_by uuid NOT NULL,
  occurred_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, deal_id) REFERENCES deal (tenant_id, id),
  FOREIGN KEY (tenant_id, recorded_by) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX outcome_tenant_idx ON outcome (tenant_id);
CREATE INDEX outcome_deal_id_idx ON outcome (deal_id);
CREATE INDEX outcome_recorded_by_idx ON outcome (recorded_by);

CREATE TABLE spend_snapshot (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  campaign_id uuid NOT NULL,
  ad_id uuid,
  day date NOT NULL,
  spend numeric(14,4) NOT NULL,
  impressions bigint,
  clicks bigint,
  pulled_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, campaign_id, ad_id, day),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES campaign (tenant_id, id),
  FOREIGN KEY (tenant_id, ad_id) REFERENCES ad (tenant_id, id)
);
CREATE INDEX spend_snapshot_tenant_idx ON spend_snapshot (tenant_id);
CREATE INDEX spend_snapshot_campaign_id_idx ON spend_snapshot (campaign_id);
CREATE INDEX spend_snapshot_ad_id_idx ON spend_snapshot (ad_id);

CREATE TABLE spend_proposal (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  state text NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed', 'signed', 'declined', 'expired', 'applied', 'verified', 'apply_failed')),
  campaign_id uuid NOT NULL,
  ad_id uuid,
  from_daily numeric(14,4) NOT NULL,
  to_daily numeric(14,4) NOT NULL,
  reason text NOT NULL,
  evidence_window text NOT NULL,
  rule_fired text NOT NULL,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, campaign_id) REFERENCES campaign (tenant_id, id),
  FOREIGN KEY (tenant_id, ad_id) REFERENCES ad (tenant_id, id)
);
CREATE INDEX spend_proposal_tenant_idx ON spend_proposal (tenant_id);
CREATE INDEX spend_proposal_campaign_id_idx ON spend_proposal (campaign_id);
CREATE INDEX spend_proposal_ad_id_idx ON spend_proposal (ad_id);

CREATE TABLE autonomy_state (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  task_type text NOT NULL,
  level smallint NOT NULL DEFAULT 0 CHECK (level IN (0, 1, 2, 3, 4)),
  window_items bigint NOT NULL DEFAULT 0,
  defects bigint NOT NULL DEFAULT 0,
  escaped bigint NOT NULL DEFAULT 0,
  canaries_shown bigint NOT NULL DEFAULT 0,
  canaries_caught bigint NOT NULL DEFAULT 0,
  paused boolean NOT NULL DEFAULT false,
  changed_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, task_type)
);
CREATE INDEX autonomy_state_tenant_idx ON autonomy_state (tenant_id);

CREATE TABLE prompt_version (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  name text NOT NULL,
  version bigint NOT NULL,
  role text NOT NULL CHECK (role IN ('plan', 'brief', 'draft', 'extract', 'classify', 'grade', 'second_read', 'memo')),
  body_sha256 text NOT NULL,
  model_id text NOT NULL,
  eval_passed boolean NOT NULL DEFAULT false,
  activated_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name, version)
);
CREATE INDEX prompt_version_tenant_idx ON prompt_version (tenant_id);

CREATE TABLE incident (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  severity text NOT NULL CHECK (severity IN ('low', 'high', 'hard_rule')),
  title text NOT NULL,
  subject_kind text,
  subject_id text,
  note text,
  opened_at timestamptz NOT NULL,
  closed_at timestamptz,
  UNIQUE (tenant_id, id)
);
CREATE INDEX incident_tenant_idx ON incident (tenant_id);

CREATE TABLE cost_ledger (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  capability text NOT NULL CHECK (capability IN ('text', 'image', 'video', 'transcription', 'voiceover', 'publishing', 'calendar', 'crm', 'messaging', 'booking', 'analytics', 'ads', 'research', 'storage', 'notifications', 'automation_hooks')),
  provider text NOT NULL,
  model text,
  units numeric(14,4) NOT NULL DEFAULT 0,
  cost_eur numeric(14,4) NOT NULL,
  run_ref text NOT NULL,
  subject_kind text,
  subject_id text,
  occurred_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id)
);
CREATE INDEX cost_ledger_tenant_idx ON cost_ledger (tenant_id);

CREATE TABLE audit_log (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  seq bigint NOT NULL,
  actor_kind text NOT NULL CHECK (actor_kind IN ('human', 'system', 'agent')),
  actor_id text NOT NULL,
  action text NOT NULL,
  subject_kind text,
  subject_id text,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  prev_hash text,
  entry_hash text NOT NULL,
  occurred_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, seq)
);
CREATE INDEX audit_log_tenant_idx ON audit_log (tenant_id);

CREATE TABLE event_log (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  name text NOT NULL,
  version bigint NOT NULL DEFAULT 1,
  actor_kind text NOT NULL CHECK (actor_kind IN ('human', 'system', 'agent')),
  actor_id text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id)
);
CREATE INDEX event_log_tenant_idx ON event_log (tenant_id);
CREATE INDEX event_log_name_idx ON event_log (name);

CREATE TABLE event_delivery (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  event_id uuid NOT NULL,
  consumer text NOT NULL,
  attempts bigint NOT NULL DEFAULT 0,
  delivered_at timestamptz,
  last_error text,
  UNIQUE (tenant_id, id),
  UNIQUE (event_id, consumer),
  FOREIGN KEY (tenant_id, event_id) REFERENCES event_log (tenant_id, id)
);
CREATE INDEX event_delivery_tenant_idx ON event_delivery (tenant_id);
CREATE INDEX event_delivery_event_id_idx ON event_delivery (event_id);

CREATE TABLE job (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL,
  state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed', 'dead')),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  run_at timestamptz NOT NULL,
  attempts bigint NOT NULL DEFAULT 0,
  max_attempts bigint NOT NULL DEFAULT 5,
  idempotency_key text,
  locked_by text,
  locked_at timestamptz,
  last_error text,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX job_tenant_idx ON job (tenant_id);
CREATE INDEX job_run_at_idx ON job (run_at);
