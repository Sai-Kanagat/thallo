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
