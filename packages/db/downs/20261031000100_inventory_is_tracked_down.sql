-- 20261031000100_inventory_is_tracked_down.sql
-- Paired down for the is_tracked flag (F4). Pure addition — drop in reverse.

DROP INDEX IF EXISTS idx_branch_inventory_untracked;
ALTER TABLE branch_inventory DROP COLUMN IF EXISTS is_tracked;
