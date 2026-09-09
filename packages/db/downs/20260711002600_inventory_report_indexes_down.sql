-- Down migration for 20260711002600_inventory_report_indexes.sql
DROP INDEX IF EXISTS idx_movements_branch_type_created;
