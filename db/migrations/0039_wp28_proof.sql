-- 0039_wp28_proof.sql (WP-28): proof, referral and customer voice. No shared table changes.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- proof_grant     what a won customer agreed to (quote, name, photo, result), bound to one consent
--                 row naming that customer, recorded by a person with evidence.
-- proof_material  one quote (an exact transcript slice and its hash), photo or verified result.
--                 consent_id is NOT NULL: the database refuses material without a consent.
-- proof_piece     a quote card or case story drafted only from material; consent_id NOT NULL.
-- proof_ask       a review or referral ask: a lead desk message, sent only through the desk.
-- proof_flag      a piece whose consent was withdrawn; live means a publication exists and a
--                 person must take it down.
-- buyer_note      the customer's own phrases (no digits) for briefs, as untrusted data.
-- Nothing is deleted by the app role. No role attributes are named, so it runs on Supabase.

CREATE TABLE proof_grant (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  consent_id uuid NOT NULL,
  person_id uuid NOT NULL,
  deal_id uuid NOT NULL,
  uses jsonb NOT NULL CHECK (jsonb_typeof(uses) = 'array' AND jsonb_array_length(uses) > 0),
  name_shown text CHECK (name_shown IS NULL OR length(name_shown) BETWEEN 1 AND 80),
  source_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  recorded_by uuid NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, consent_id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, person_id) REFERENCES person (tenant_id, id),
  FOREIGN KEY (tenant_id, deal_id) REFERENCES deal (tenant_id, id),
  FOREIGN KEY (tenant_id, recorded_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('proof_grant');

CREATE TABLE proof_material (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL CHECK (kind IN ('quote', 'photo', 'result')),
  grant_id uuid NOT NULL,
  consent_id uuid NOT NULL,
  person_id uuid NOT NULL,
  deal_id uuid NOT NULL,
  source_id uuid,
  span_start integer,
  span_end integer,
  text text,
  text_sha256 text CHECK (text_sha256 IS NULL OR text_sha256 ~ '^[0-9a-f]{64}$'),
  media_id uuid,
  claim_id uuid,
  state text NOT NULL CHECK (state IN ('live', 'withdrawn')),
  withdrawn_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  CHECK (kind <> 'quote' OR (source_id IS NOT NULL AND text IS NOT NULL AND text_sha256 IS NOT NULL
                             AND span_start >= 0 AND span_end > span_start)),
  CHECK (kind <> 'photo' OR media_id IS NOT NULL),
  CHECK (kind <> 'result' OR claim_id IS NOT NULL),
  FOREIGN KEY (tenant_id, grant_id) REFERENCES proof_grant (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, source_id) REFERENCES source (tenant_id, id),
  FOREIGN KEY (tenant_id, media_id) REFERENCES media (tenant_id, id),
  FOREIGN KEY (tenant_id, claim_id) REFERENCES claim (tenant_id, id)
);
CREATE INDEX proof_material_consent_idx ON proof_material (tenant_id, consent_id);
SELECT app_enable_tenant_rls('proof_material');

CREATE TABLE proof_piece (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL CHECK (kind IN ('quote_card', 'case_story')),
  deal_id uuid NOT NULL,
  person_id uuid NOT NULL,
  consent_id uuid NOT NULL,
  material_ids jsonb NOT NULL CHECK (jsonb_typeof(material_ids) = 'array' AND jsonb_array_length(material_ids) > 0),
  claim_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  body text NOT NULL,
  card jsonb NOT NULL DEFAULT '{}'::jsonb,
  state text NOT NULL CHECK (state IN ('live', 'withdrawn')),
  variant_id uuid,
  withdrawn_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, deal_id) REFERENCES deal (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id)
);
CREATE INDEX proof_piece_consent_idx ON proof_piece (tenant_id, consent_id);
SELECT app_enable_tenant_rls('proof_piece');

CREATE TABLE proof_ask (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  kind text NOT NULL CHECK (kind IN ('review', 'referral')),
  lead_id uuid NOT NULL,
  message_id uuid NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, lead_id, kind),
  UNIQUE (tenant_id, message_id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES lead (tenant_id, id),
  FOREIGN KEY (tenant_id, message_id) REFERENCES message (tenant_id, id)
);
SELECT app_enable_tenant_rls('proof_ask');

CREATE TABLE proof_flag (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  piece_id uuid NOT NULL,
  consent_id uuid NOT NULL,
  variant_id uuid,
  live boolean NOT NULL,
  resolved_by uuid,
  resolved_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, piece_id),
  FOREIGN KEY (tenant_id, piece_id) REFERENCES proof_piece (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id),
  FOREIGN KEY (tenant_id, resolved_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('proof_flag');

CREATE TABLE buyer_note (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  grant_id uuid NOT NULL,
  consent_id uuid NOT NULL,
  source_id uuid NOT NULL,
  span_start integer NOT NULL CHECK (span_start >= 0),
  span_end integer NOT NULL CHECK (span_end > span_start),
  phrase text NOT NULL CHECK (length(phrase) BETWEEN 1 AND 300 AND phrase !~ '[0-9]'),
  state text NOT NULL CHECK (state IN ('live', 'withdrawn')),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, source_id, span_start, span_end),
  FOREIGN KEY (tenant_id, grant_id) REFERENCES proof_grant (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, source_id) REFERENCES source (tenant_id, id)
);
SELECT app_enable_tenant_rls('buyer_note');

REVOKE DELETE, TRUNCATE ON proof_grant, proof_material, proof_piece, proof_ask, proof_flag, buyer_note
  FROM cortex_app;
REVOKE UPDATE ON proof_grant, proof_ask FROM cortex_app;
