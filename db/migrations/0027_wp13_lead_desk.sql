-- 0027_wp13_lead_desk.sql (WP-13): the lead desk's own tables. No shared table changes.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- lead_desk_setting  one row per tenant: the play, the per-tenant lists the fit rules need
--                    (service area, stock, supported use cases) and the first-response clock.
-- lead_action        the actions heat decays over (a chat, a call, a form, a link click).
-- lead_message_send  one row per message a person approved for sending, same id as the message.
--                    It binds the exact channel, recipient and body hash the person saw, the
--                    consent record checked at that moment and the person who tapped. The primary
--                    key makes a second tap on the same message fail. Never deleted.
-- No role attributes are named, so it runs on Supabase as it does on Docker.

CREATE TABLE lead_desk_setting (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  play_code text NOT NULL CHECK (play_code ~ '^[A-Z]{1,4}$'),
  service_area jsonb NOT NULL DEFAULT '[]'::jsonb,
  stock jsonb NOT NULL DEFAULT '[]'::jsonb,
  supported_use_cases jsonb NOT NULL DEFAULT '[]'::jsonb,
  response_minutes integer NOT NULL DEFAULT 60 CHECK (response_minutes BETWEEN 5 AND 10080),
  acknowledgement_template_id uuid,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id),
  FOREIGN KEY (tenant_id, acknowledgement_template_id) REFERENCES message_template (tenant_id, id)
);
SELECT app_enable_tenant_rls('lead_desk_setting');

CREATE TABLE lead_action (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  lead_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]{0,39}$'),
  touch_id uuid,
  occurred_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES lead (tenant_id, id),
  FOREIGN KEY (tenant_id, touch_id) REFERENCES touch (tenant_id, id)
);
CREATE INDEX lead_action_lead_idx ON lead_action (tenant_id, lead_id);
SELECT app_enable_tenant_rls('lead_action');

CREATE TABLE lead_message_send (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  channel text NOT NULL CHECK (channel IN ('whatsapp', 'email', 'sms')),
  recipient text NOT NULL,
  body_sha256 text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
  consent_id uuid,
  approved_by uuid NOT NULL,
  approved_at timestamptz NOT NULL,
  state text NOT NULL CHECK (state IN ('approved', 'opened', 'sent', 'failed')),
  external_id text,
  detail text,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, id) REFERENCES message (tenant_id, id),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id),
  FOREIGN KEY (tenant_id, approved_by) REFERENCES app_user (tenant_id, id)
);
SELECT app_enable_tenant_rls('lead_message_send');
REVOKE DELETE, TRUNCATE ON lead_message_send FROM cortex_app;

-- consent_recipient  the exact address or number a contact consent covers. Consent is checked per
--                    recipient, not per person: a consent with no row here allows no message.
CREATE TABLE consent_recipient (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  consent_id uuid NOT NULL,
  channel text NOT NULL CHECK (channel IN ('whatsapp', 'email', 'sms')),
  recipient text NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, consent_id, channel, recipient),
  FOREIGN KEY (tenant_id, consent_id) REFERENCES consent (tenant_id, id)
);
SELECT app_enable_tenant_rls('consent_recipient');

-- lead_contact_candidate  a number or address an unverified touch named that the matched person
--                         does not already have. Never merged onto the person by the machine.
CREATE TABLE lead_contact_candidate (
  id uuid NOT NULL,
  created_at timestamptz NOT NULL,
  tenant_id uuid NOT NULL REFERENCES tenant(id),
  lead_id uuid NOT NULL,
  channel text NOT NULL CHECK (channel IN ('whatsapp', 'email', 'sms')),
  value text NOT NULL,
  touch_id uuid,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, lead_id) REFERENCES lead (tenant_id, id),
  FOREIGN KEY (tenant_id, touch_id) REFERENCES touch (tenant_id, id)
);
SELECT app_enable_tenant_rls('lead_contact_candidate');
