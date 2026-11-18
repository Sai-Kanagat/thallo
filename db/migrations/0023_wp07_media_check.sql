-- 0023_wp07_media_check.sql (WP-07, lead ruling on media coverage): a check row can name the one
-- media item it checked.
-- No BEGIN or COMMIT: scripts/migrate.py wraps this file in one transaction.
--
-- media_sha256 is the sha256 of the media file's bytes (the same value the bundle root hashes for
-- that item). The gate counts a media check toward an attached item only when this equals the
-- item's bytes hash, so every attached file needs its own check rows (gate/checks.py
-- MEDIA_COVERAGE). Text checks leave it NULL. Existing rows stay NULL and so never cover media:
-- a variant with media is "not checked" until the check stack (thallo/checks) runs on it again.
-- check_result stays append-only (0021): this adds a nullable column and nothing else. No role
-- attributes are named, so it runs on Supabase as it does on Docker.

ALTER TABLE check_result ADD COLUMN media_sha256 text;
ALTER TABLE check_result ADD CONSTRAINT check_result_media_sha256_hex
  CHECK (media_sha256 IS NULL OR media_sha256 ~ '^[0-9a-f]{64}$');

-- Where a media file came from is fixed when the row is written (WP-07 security review): the gate
-- picks the checks an item needs from its provenance, and the canonical form (v2) hashes it. A
-- trigger refuses any UPDATE that changes it, whoever runs it; changing that takes a migration
-- run by the owner. A file from another source is a new media row.
CREATE FUNCTION media_provenance_immutable() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  IF NEW.provenance IS DISTINCT FROM OLD.provenance THEN
    RAISE EXCEPTION 'media.provenance is fixed at insert'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION media_provenance_immutable() FROM PUBLIC;

CREATE TRIGGER media_provenance_immutable BEFORE UPDATE OF provenance ON media
  FOR EACH ROW EXECUTE FUNCTION media_provenance_immutable();
