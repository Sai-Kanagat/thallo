-- 0021_check_content_hash.sql (lead ruling, contracts 2.5.0): check results are bound to content.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- Every check_result row now names the content it checked: content_sha256 is the variant's leaf
-- hash in the bundle root (thallo.gate.variant_hash). The gate counts only rows whose hash is the
-- variant's current one, so fixing the content is what clears a FAIL, and restoring the old
-- content brings the old FAIL back.
--
-- Existing rows. They were written before any row named its content, so nothing can say which
-- content they checked. They get the marker 'unbound-before-0021', which is not 64 hex digits and
-- so can never equal a real hash: old rows stop counting, for or against. A variant whose only
-- rows are old is "not checked" and its grant is refused until the code checks run again on the
-- current content. Nothing is deleted.
--
-- The two CHECK constraints are NOT VALID: they bind every new row, while the old rows (marker
-- hash, and whatever attempt they hold) are left as they are, inert. Rows cannot be updated
-- afterwards, so an old row can never be turned into a counting one.

ALTER TABLE check_result ADD COLUMN content_sha256 text NOT NULL DEFAULT 'unbound-before-0021';
ALTER TABLE check_result ALTER COLUMN content_sha256 DROP DEFAULT;
ALTER TABLE check_result ADD CONSTRAINT check_result_content_sha256_hex
  CHECK (content_sha256 ~ '^[0-9a-f]{64}$') NOT VALID;
ALTER TABLE check_result ADD CONSTRAINT check_result_attempt_positive CHECK (attempt >= 1) NOT VALID;

-- The gate reads one variant's rows for one hash.
CREATE INDEX check_result_variant_content_idx ON check_result (tenant_id, variant_id, content_sha256);

-- Append-only for the app role: a check result is evidence, like the ledgers in 0010.
REVOKE UPDATE, DELETE, TRUNCATE ON check_result FROM cortex_app;

-- And it holds even if a grant comes back later: the trigger refuses every UPDATE and DELETE,
-- whoever runs it. Changing that takes a migration that drops the trigger, run by the owner.
CREATE FUNCTION check_result_append_only() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION 'check_result is append-only: % refused', TG_OP
    USING ERRCODE = 'insufficient_privilege';
END
$$;
REVOKE ALL ON FUNCTION check_result_append_only() FROM PUBLIC;

CREATE TRIGGER check_result_append_only BEFORE UPDATE OR DELETE ON check_result
  FOR EACH ROW EXECUTE FUNCTION check_result_append_only();
CREATE TRIGGER check_result_no_truncate BEFORE TRUNCATE ON check_result
  FOR EACH STATEMENT EXECUTE FUNCTION check_result_append_only();
