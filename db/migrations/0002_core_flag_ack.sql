-- 0002_core_flag_ack.sql (lead, contracts 2.2.0).
-- Review flags must be acknowledged: an approval records which FLAG check results the approver
-- explicitly accepted, and the gate refuses a grant that leaves any open flag unacknowledged.
ALTER TABLE approval ADD COLUMN acknowledged_flag_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
