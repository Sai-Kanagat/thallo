-- 0030_wp20_trust.sql (WP-20): trust ladder, reviewer canaries, cost breakers, weekly hand audit.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- autonomy_state, incident and cost_ledger are core (0001). These tables hold what the ladder
-- needs beyond those counters. Every tenant table calls app_enable_tenant_rls.
--
-- Spend totals. A trigger on cost_ledger keeps two running totals, atomically (one upsert):
--   cost_day_total     per tenant, UTC day and provider, tenant-scoped like any table;
--   cost_global_day    per UTC day and provider, across tenants. Above tenants: RLS enabled
--                      with no policy and no grant to the app role (like operator, identity).
-- The app role reads the global totals only through app_global_spend(day), which returns
-- provider sums and nothing about which tenant spent them. Both functions are SECURITY DEFINER
-- with a fixed search_path; neither names a role attribute, so this runs on Supabase as on Docker.

CREATE TABLE trust_task (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  task_type text NOT NULL CHECK (length(task_type) BETWEEN 1 AND 80),
  version_key text,
  version_changed_at timestamptz,
  clean_since_version bigint NOT NULL DEFAULT 0 CHECK (clean_since_version >= 0),
  calibration_done boolean NOT NULL DEFAULT false,
  voice_rules_signed_by uuid,
  template_sha256 text CHECK (template_sha256 IS NULL OR template_sha256 ~ '^[0-9a-f]{64}$'),
  template_signed_by uuid,
  pause_reason text,
  updated_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, task_type),
  CHECK ((template_sha256 IS NULL) = (template_signed_by IS NULL)),
  FOREIGN KEY (tenant_id, voice_rules_signed_by) REFERENCES app_user (tenant_id, id),
  FOREIGN KEY (tenant_id, template_signed_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('trust_task');

CREATE TABLE trust_item (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  task_type text NOT NULL,
  subject_kind text NOT NULL,
  subject_id text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('clean', 'typo', 'defect', 'escaped', 'hard_rule')),
  version_key text,
  note text,
  occurred_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, task_type, subject_id)
);
SELECT app_enable_tenant_rls('trust_item');
CREATE INDEX trust_item_recent_idx ON trust_item (tenant_id, task_type, occurred_at DESC);

CREATE TABLE trust_canary (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  task_type text NOT NULL,
  variant_id uuid NOT NULL,
  flaw text NOT NULL CHECK (flaw IN ('swapped_claim', 'banned_phrase', 'off_brand_image')),
  state text NOT NULL CHECK (state IN ('planted', 'caught', 'missed')),
  planted_at timestamptz NOT NULL,
  resolved_at timestamptz,
  resolved_by uuid,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, variant_id),
  CHECK ((state = 'planted') = (resolved_at IS NULL)),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, resolved_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('trust_canary');
CREATE INDEX trust_canary_task_idx ON trust_canary (tenant_id, task_type);

CREATE TABLE cost_cap (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  scope text NOT NULL CHECK (scope IN ('run', 'day')),
  provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 80),
  cap_eur numeric(14,4) NOT NULL CHECK (cap_eur >= 0),
  set_by text NOT NULL,
  set_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, scope, provider)
);
SELECT app_enable_tenant_rls('cost_cap');

CREATE TABLE breaker_trip (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  scope text NOT NULL CHECK (scope IN ('run', 'day')),
  subject text NOT NULL,
  run_ref text,
  task_type text,
  day date NOT NULL,
  cap_eur numeric(14,4) NOT NULL,
  spent_eur numeric(14,4) NOT NULL,
  tripped_at timestamptz NOT NULL,
  reset_at timestamptz,
  reset_by uuid,
  UNIQUE (tenant_id, id),
  CHECK ((reset_at IS NULL) = (reset_by IS NULL)),
  FOREIGN KEY (tenant_id, reset_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('breaker_trip');
CREATE INDEX breaker_trip_day_idx ON breaker_trip (tenant_id, day);

CREATE TABLE trust_audit_sentence (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  week_start date NOT NULL CHECK (extract(isodow FROM week_start) = 1),
  position smallint NOT NULL CHECK (position BETWEEN 1 AND 20),
  variant_id uuid NOT NULL,
  sentence text NOT NULL CHECK (length(sentence) BETWEEN 1 AND 2000),
  verdict text NOT NULL CHECK (verdict IN ('pending', 'correct', 'wrong')),
  note text CHECK (note IS NULL OR length(note) <= 500),
  checked_by uuid,
  checked_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, week_start, position),
  CHECK ((verdict = 'pending') = (checked_by IS NULL)),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, checked_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('trust_audit_sentence');

-- ------------------------------------------------------------------ spend totals

-- A cost is never negative: a negative row would lower the totals and loosen every cap.
-- Refunds go through an operator path, never through cost_ledger.
ALTER TABLE cost_ledger ADD CONSTRAINT cost_ledger_cost_not_negative CHECK (cost_eur >= 0);

CREATE TABLE cost_day_total (
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  day date NOT NULL,
  provider text NOT NULL,
  total_eur numeric(16,4) NOT NULL,
  PRIMARY KEY (tenant_id, day, provider)
);
SELECT app_enable_tenant_rls('cost_day_total');
REVOKE INSERT, UPDATE, DELETE ON cost_day_total FROM cortex_app;  -- the trigger writes it

CREATE TABLE cost_global_day (
  day date NOT NULL,
  provider text NOT NULL,
  total_eur numeric(16,4) NOT NULL,
  PRIMARY KEY (day, provider)
);
ALTER TABLE cost_global_day ENABLE ROW LEVEL SECURITY;  -- no policy: the owner-run functions only
REVOKE ALL ON cost_global_day FROM PUBLIC;

CREATE FUNCTION app_cost_totals() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
  d date := (NEW.occurred_at AT TIME ZONE 'UTC')::date;
BEGIN
  INSERT INTO cost_day_total (tenant_id, day, provider, total_eur)
  VALUES (NEW.tenant_id, d, NEW.provider, NEW.cost_eur)
  ON CONFLICT (tenant_id, day, provider) DO UPDATE SET total_eur = cost_day_total.total_eur + EXCLUDED.total_eur;
  INSERT INTO cost_global_day (day, provider, total_eur)
  VALUES (d, NEW.provider, NEW.cost_eur)
  ON CONFLICT (day, provider) DO UPDATE SET total_eur = cost_global_day.total_eur + EXCLUDED.total_eur;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION app_cost_totals() FROM PUBLIC;

CREATE TRIGGER cost_ledger_totals AFTER INSERT ON cost_ledger
FOR EACH ROW EXECUTE FUNCTION app_cost_totals();

CREATE FUNCTION app_global_spend(d date) RETURNS TABLE (provider text, total_eur numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$ SELECT g.provider, g.total_eur FROM cost_global_day g WHERE g.day = d $$;
REVOKE ALL ON FUNCTION app_global_spend(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_global_spend(date) TO cortex_app;
-- Supabase exposes public functions to its API roles; none of them may call these.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON FUNCTION app_global_spend(date) FROM %I', r);
      EXECUTE format('REVOKE ALL ON FUNCTION app_cost_totals() FROM %I', r);
      EXECUTE format('REVOKE ALL ON TABLE cost_global_day FROM %I', r);
    END IF;
  END LOOP;
END
$$;
