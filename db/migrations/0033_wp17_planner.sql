-- 0033_wp17_planner.sql (WP-17): the planner's week, its slots and calendar mirror runs.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- Core tables (idea, brief, forecast, forecast_score, plan_slot) are used unchanged. These rows
-- hold what core has no column for: the week's two reasoning lines and what it chose not to do,
-- the play, target metric and format behind each slot, and every mirror run. A forecast is never
-- edited after it is sealed (forecast in core); planner_slot only points at it.
-- No shared table changes. No role attributes are named, so it runs on Supabase as on Docker.

CREATE TABLE planner_week (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  week_start date NOT NULL CHECK (extract(isodow FROM week_start) = 1),
  play_code text NOT NULL CHECK (play_code ~ '^[A-Z](-[a-z0-9]{1,16})?$'),
  growth_model_version integer NOT NULL CHECK (growth_model_version >= 1),
  reasoning jsonb NOT NULL CHECK (jsonb_typeof(reasoning) = 'array' AND jsonb_array_length(reasoning) = 2),
  not_doing jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(not_doing) = 'array'),
  stage_check jsonb NOT NULL DEFAULT '{}'::jsonb,
  planned_slots integer NOT NULL CHECK (planned_slots >= 0),
  experiment_slots integer NOT NULL CHECK (experiment_slots >= 0 AND experiment_slots <= planned_slots),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, week_start)
);
SELECT app_enable_tenant_rls('planner_week');

CREATE TABLE planner_slot (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  week_id uuid NOT NULL,
  slot_id uuid NOT NULL,
  idea_id uuid NOT NULL,
  brief_id uuid NOT NULL,
  forecast_id uuid,
  no_forecast_reason text CHECK (no_forecast_reason IS NULL OR length(no_forecast_reason) <= 300),
  play_code text NOT NULL,
  target_metric text NOT NULL CHECK (target_metric ~ '^[a-z][a-z0-9_]{0,63}$'),
  metric_day integer NOT NULL CHECK (metric_day IN (2, 7, 14)),
  format text NOT NULL CHECK (format ~ '^[a-z][a-z0-9_]{0,39}$'),
  reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, slot_id),
  CHECK ((forecast_id IS NULL) <> (no_forecast_reason IS NULL)),
  FOREIGN KEY (tenant_id, week_id) REFERENCES planner_week (tenant_id, id),
  FOREIGN KEY (tenant_id, slot_id) REFERENCES plan_slot (tenant_id, id),
  FOREIGN KEY (tenant_id, idea_id) REFERENCES idea (tenant_id, id),
  FOREIGN KEY (tenant_id, brief_id) REFERENCES brief (tenant_id, id),
  FOREIGN KEY (tenant_id, forecast_id) REFERENCES forecast (tenant_id, id)
);
SELECT app_enable_tenant_rls('planner_slot');
CREATE INDEX planner_slot_week_idx ON planner_slot (tenant_id, week_id);

CREATE TABLE planner_mirror (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  week_start date NOT NULL,
  provider text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_-]{0,63}$'),
  mode text NOT NULL,
  status text NOT NULL CHECK (status IN ('ok', 'failed')),
  mirrored integer NOT NULL CHECK (mirrored >= 0),
  detail text CHECK (detail IS NULL OR length(detail) <= 500),
  mirrored_at timestamptz NOT NULL,
  UNIQUE (tenant_id, id)
);
SELECT app_enable_tenant_rls('planner_mirror');

-- Mirror runs are a record of what went out: append-only.
REVOKE UPDATE, DELETE, TRUNCATE ON planner_mirror FROM cortex_app;

-- ---------------------------------------------------------------- sealed forecasts stay sealed
-- forecast and forecast_score (core) become append-only for everyone: a trigger refuses UPDATE,
-- DELETE and TRUNCATE whoever runs them, and the app role loses those grants as well.
CREATE FUNCTION forecast_append_only() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION '% is append-only: % refused', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END
$$;
REVOKE ALL ON FUNCTION forecast_append_only() FROM PUBLIC;

CREATE TRIGGER forecast_append_only BEFORE UPDATE OR DELETE ON forecast
  FOR EACH ROW EXECUTE FUNCTION forecast_append_only();
CREATE TRIGGER forecast_no_truncate BEFORE TRUNCATE ON forecast
  FOR EACH STATEMENT EXECUTE FUNCTION forecast_append_only();
CREATE TRIGGER forecast_score_append_only BEFORE UPDATE OR DELETE ON forecast_score
  FOR EACH ROW EXECUTE FUNCTION forecast_append_only();
CREATE TRIGGER forecast_score_no_truncate BEFORE TRUNCATE ON forecast_score
  FOR EACH STATEMENT EXECUTE FUNCTION forecast_append_only();
REVOKE UPDATE, DELETE, TRUNCATE ON forecast, forecast_score FROM cortex_app;

-- The chain head, one row per tenant, written only by the trigger below (owner rights). The app
-- role may read it and nothing else, so dropping the newest forecasts cannot go unnoticed:
-- verify_chain checks the head's seq and hash are the newest forecast on record.
CREATE TABLE forecast_head (
  id uuid PRIMARY KEY,            -- the tenant id: one head per tenant
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL UNIQUE REFERENCES tenant(id),
  seq integer NOT NULL CHECK (seq >= 1),
  seal_hash text NOT NULL CHECK (seal_hash ~ '^[0-9a-f]{64}$'),
  updated_at timestamptz NOT NULL,
  CHECK (id = tenant_id)
);
SELECT app_enable_tenant_rls('forecast_head');
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON forecast_head FROM cortex_app;

-- Every new forecast must extend the chain: seq one past the head, prev_hash the head's hash.
-- SECURITY DEFINER so it can move the head the app role cannot write; the tenant policy still
-- applies (FORCE ROW LEVEL SECURITY) because the session's tenant is the row's tenant.
CREATE FUNCTION forecast_extend_head() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  h public.forecast_head%ROWTYPE;
BEGIN
  SELECT * INTO h FROM public.forecast_head WHERE id = NEW.tenant_id FOR UPDATE;
  IF NOT FOUND THEN
    IF NEW.seq <> 1 OR NEW.prev_hash IS NOT NULL THEN
      RAISE EXCEPTION 'forecast chain: the first forecast must be seq 1 with no prev_hash'
        USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO public.forecast_head (id, created_at, tenant_id, seq, seal_hash, updated_at)
      VALUES (NEW.tenant_id, NEW.created_at, NEW.tenant_id, NEW.seq, NEW.seal_hash, NEW.created_at);
  ELSE
    IF NEW.seq <> h.seq + 1 OR NEW.prev_hash IS DISTINCT FROM h.seal_hash THEN
      RAISE EXCEPTION 'forecast chain: seq % does not extend the head %', NEW.seq, h.seq
        USING ERRCODE = 'check_violation';
    END IF;
    UPDATE public.forecast_head SET seq = NEW.seq, seal_hash = NEW.seal_hash, updated_at = NEW.created_at
      WHERE id = NEW.tenant_id;
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION forecast_extend_head() FROM PUBLIC;
CREATE TRIGGER forecast_extend_head BEFORE INSERT ON forecast
  FOR EACH ROW EXECUTE FUNCTION forecast_extend_head();
