-- 20260711003100_shelf_location_norm_down.sql
-- Reverses OBJECTS only. Data normalization (A-3->A3, invalid->NULL) is NOT reversed.
DROP INDEX IF EXISTS idx_inventory_branch_shelf_prefix;
ALTER TABLE branch_inventory DROP CONSTRAINT IF EXISTS chk_shelf_format;
DROP TRIGGER IF EXISTS trg_branch_inventory_shelf ON branch_inventory;
DROP FUNCTION IF EXISTS trg_normalize_shelf();
DROP FUNCTION IF EXISTS normalize_shelf_code(TEXT);
