-- 0040_wp29_founder_newsletter.sql (WP-29): founder profile desk and the newsletter.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- founder_post           a personal-profile variant and the founder's own recorded answers it was written
--                        from. At least one answer on every row (NOT NULL, non-empty): a post with no answer
--                        of the founder behind it cannot be recorded, so no ghost views. Append-only.
-- newsletter_issue       one fortnightly issue per tenant per period, the variant (channel newsletter) that
--                        goes through the check stack and the gate, and the pieces it was built from.
-- newsletter_subscriber  a subscription: one exact email address, the contact consent that covers it and a
--                        random unsubscribe token. Unsubscribing sets unsubscribed_at and withdraws the consent.
-- newsletter_delivery    one row per issue per subscriber, written at the human tap before the provider call,
--                        so a second tap sends nothing twice. It names the consent checked at that moment.
--
-- No role attributes are named, so it runs on Supabase as it does on Docker.

CREATE TABLE founder_post (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  founder_id uuid NOT NULL,
  variant_id uuid NOT NULL,
  answer_ids jsonb NOT NULL CHECK (jsonb_typeof(answer_ids) = 'array' AND jsonb_array_length(answer_ids) >= 1),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, variant_id),
  FOREIGN KEY (tenant_id, founder_id) REFERENCES app_user (tenant_id, id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id)
);
SELECT app_enable_tenant_rls('founder_post');
REVOKE UPDATE, DELETE, TRUNCATE ON founder_post FROM cortex_app;

CREATE TABLE newsletter_issue (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  period_start date NOT NULL,
  period_end date NOT NULL CHECK (period_end > period_start),
  variant_id uuid NOT NULL,
  piece_brief_ids jsonb NOT NULL
    CHECK (jsonb_typeof(piece_brief_ids) = 'array' AND jsonb_array_length(piece_brief_ids) >= 1),
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, period_start),
  UNIQUE (tenant_id, variant_id),
  FOREIGN KEY (tenant_id, variant_id) REFERENCES variant (tenant_id, id)
);
SELECT app_enable_tenant_rls('newsletter_issue');
REVOKE UPDATE, DELETE, TRUNCATE ON newsletter_issue FROM cortex_app;

CREATE TABLE newsletter_subscriber (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  person_id uuid NOT NULL,
  consent_id uuid NOT NULL,
  recipient text NOT NULL CHECK (recipient = lower(recipient) AND recipient LIKE '%_@_%'),
  token text NOT NULL CHECK (token ~ '^[0-9a-f]{64}$'),
  unsubscribed_at timestamptz,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, recipient),
  UNIQUE (token),
  FOREIGN KEY (tenant_id, person_id) REFERENCES person (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id)
);
SELECT app_enable_tenant_rls('newsletter_subscriber');
REVOKE DELETE, TRUNCATE ON newsletter_subscriber FROM cortex_app;

CREATE TABLE newsletter_delivery (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  issue_id uuid NOT NULL,
  subscriber_id uuid NOT NULL,
  recipient text NOT NULL,
  consent_id uuid NOT NULL,
  body_sha256 text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
  approved_by uuid NOT NULL,
  approved_at timestamptz NOT NULL,
  state text NOT NULL CHECK (state IN ('approved', 'opened', 'sent', 'failed')),
  external_id text,
  detail text,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, issue_id, subscriber_id),
  FOREIGN KEY (tenant_id, issue_id) REFERENCES newsletter_issue (tenant_id, id),
  FOREIGN KEY (tenant_id, subscriber_id) REFERENCES newsletter_subscriber (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, approved_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('newsletter_delivery');
REVOKE DELETE, TRUNCATE ON newsletter_delivery FROM cortex_app;
