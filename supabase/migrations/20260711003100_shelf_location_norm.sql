-- 20260711003100_shelf_location_norm.sql
-- M0/M1 skeleton — test scratch DBs only (see M0 classification doc); NOT run here.
-- Shelf-code norm on the existing branch_inventory.shelf_location (VARCHAR(50)).
-- Policy: ASCII-only translate (no locale UPPER, IMMUTABLE-safe) + Arabic-Indic
-- digit mapping; empty -> NULL; invalid -> NULL + audit_logs (never boot-blocking).
--
-- Order inside the single enclosing transaction (apply-migrations.sh uses -1):
--   LOCK -> normalize valid/blank -> audit invalids -> null invalids
--   -> re-assert -> trigger -> plain CHECK -> prefix index.
-- The down file reverses objects only; data normalization is NOT reversed.
--
-- Encoding note: no literal NBSP anywhere in this file (mojibake guard, P3-M8).
-- NBSP is stripped via CHR(160), never as a pasted byte.

-- 0) Freeze writers for the table duration (single-branch device: full block OK).
LOCK TABLE branch_inventory IN ACCESS EXCLUSIVE MODE;

-- 1) ASCII-only normalizer (shared vectors: packages/shared/src/shelf_location_vectors.json).
CREATE OR REPLACE FUNCTION normalize_shelf_code(input TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
  SELECT NULLIF(
    regexp_replace(
      regexp_replace(
        translate(coalesce(input, ''),
          'abcdefghijklmnopqrstuvwxyz٠١٢٣٤٥٦٧٨٩',
          'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'),
        CHR(160), '', 'g'),
      '[\s\-_]+', '', 'g'),
    '');
$$;

-- 2) Normalize the normalizable + blanks (IS DISTINCT FROM avoids touching clean rows,
--    so touch_row does not bump sync_version/updated_at needlessly).
UPDATE branch_inventory
SET shelf_location = normalize_shelf_code(shelf_location)
WHERE NOT is_deleted
  AND shelf_location IS DISTINCT FROM normalize_shelf_code(shelf_location)
  AND (normalize_shelf_code(shelf_location) IS NULL
       OR normalize_shelf_code(shelf_location) ~ '^[A-Z][1-9][0-9]?$');

-- 3) Invalid -> NULL + audit. ORDER MATTERS: audit FIRST from the still-present
--    old values (UPDATE..RETURNING yields the NEW row, so it would record NULL
--    as old), then null them out — same transaction, boot never aborts.
--    audit_logs.actor_user_id NULL is allowed; action VARCHAR(50) has no CHECK.
WITH bad AS (
  SELECT bi.id, bi.branch_id, bi.shelf_location AS old_loc
  FROM branch_inventory bi
  WHERE NOT bi.is_deleted
    AND bi.shelf_location IS NOT NULL
    AND normalize_shelf_code(bi.shelf_location) IS NOT NULL
    AND normalize_shelf_code(bi.shelf_location) !~ '^[A-Z][1-9][0-9]?$'
)
INSERT INTO audit_logs (action, actor_user_id, actor_username, branch_id,
                        entity_type, entity_id, metadata)
SELECT 'settings.changed', NULL, NULL, bad.branch_id,
       'branch_inventory', bad.id,
       jsonb_build_object('scope', 'shelf_location',
                          'old', bad.old_loc,
                          'new', NULL,
                          'reason', 'shelf_norm_migration')
FROM bad;
UPDATE branch_inventory bi
SET shelf_location = NULL
WHERE NOT bi.is_deleted
  AND bi.shelf_location IS NOT NULL
  AND normalize_shelf_code(bi.shelf_location) IS NOT NULL
  AND normalize_shelf_code(bi.shelf_location) !~ '^[A-Z][1-9][0-9]?$';

-- 4) Re-assert: nothing violable may remain (diagnostic abort, never silent).
DO $$
DECLARE c INT;
BEGIN
  SELECT COUNT(*) INTO c FROM branch_inventory
  WHERE NOT is_deleted AND shelf_location IS NOT NULL
    AND shelf_location !~ '^[A-Z][1-9][0-9]?$';
  IF c > 0 THEN RAISE EXCEPTION 'shelf_location: % violator(s) remain after norm', c; END IF;
END;
$$;

-- 5) Trigger normalization for future writes.
CREATE OR REPLACE FUNCTION trg_normalize_shelf() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.shelf_location := normalize_shelf_code(NEW.shelf_location);
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_branch_inventory_shelf ON branch_inventory;
CREATE TRIGGER trg_branch_inventory_shelf
  BEFORE INSERT OR UPDATE OF shelf_location ON branch_inventory
  FOR EACH ROW EXECUTE FUNCTION trg_normalize_shelf();

-- 6) Plain CHECK (NOT VALID deliberately NOT used: table is already locked+clean).
ALTER TABLE branch_inventory
  ADD CONSTRAINT chk_shelf_format
  CHECK (shelf_location IS NULL OR shelf_location ~ '^[A-Z][1-9][0-9]?$');

-- 7) Prefix filter index on the CORRECT opclass for VARCHAR (not text_pattern_ops).
CREATE INDEX idx_inventory_branch_shelf_prefix
  ON branch_inventory (branch_id, shelf_location varchar_pattern_ops)
  WHERE NOT is_deleted AND shelf_location IS NOT NULL;
