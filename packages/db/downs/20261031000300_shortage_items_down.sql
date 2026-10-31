-- 20261031000300_shortage_items_down.sql
-- Paired down for the shortages module. Pure addition — drop in reverse.

DROP INDEX IF EXISTS idx_shortage_branch_kind;
DROP INDEX IF EXISTS uq_shortage_open_name;
DROP INDEX IF EXISTS uq_shortage_open_med;
DROP TABLE IF EXISTS shortage_items;
