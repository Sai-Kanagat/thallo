-- 0003_core_consent_scopes.sql (lead, contracts 2.3.0).
-- One consent record may cover several scopes (likeness and voice for a filmed customer).
ALTER TABLE consent ADD COLUMN extra_scopes jsonb NOT NULL DEFAULT '[]'::jsonb;
