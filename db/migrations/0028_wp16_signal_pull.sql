-- 0028_wp16_signal_pull.sql (WP-16): raw pulls kept.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- One row per pull from one analytics source for one window (thallo/signals/intake.py). It keeps
-- the source's decoded reply as it came (`raw`, with its sha256), what was asked, what came back
-- missing and whether the pull worked. The per-day values become `signal` rows (kind 'metric')
-- that name this row's id in their payload. A rerun of the same window is a new row; readers take
-- the newest value per (source, metric, day). Missing is stored as missing, never as zero.
-- Append-only: a pull is the record of what a source said. No role attributes are named, so it
-- runs on Supabase as it does on Docker.

CREATE TABLE signal_pull (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  provider text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_-]{0,63}$'),
  source text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_]{0,63}$'),
  window_start date NOT NULL,
  window_end date NOT NULL,
  metrics jsonb NOT NULL DEFAULT '[]'::jsonb,
  missing jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL CHECK (status IN ('ok', 'partial', 'failed', 'manual')),
  detail text CHECK (detail IS NULL OR length(detail) <= 500),
  row_count integer NOT NULL DEFAULT 0 CHECK (row_count >= 0),
  raw jsonb,
  raw_sha256 text CHECK (raw_sha256 IS NULL OR raw_sha256 ~ '^[0-9a-f]{64}$'),
  pulled_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  CHECK (window_start <= window_end)
);
CREATE INDEX signal_pull_window_idx ON signal_pull (tenant_id, source, window_start, window_end, pulled_at);
SELECT app_enable_tenant_rls('signal_pull');

REVOKE UPDATE, DELETE, TRUNCATE ON signal_pull FROM cortex_app;
