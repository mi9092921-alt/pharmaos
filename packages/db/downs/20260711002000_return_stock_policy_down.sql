-- Down migration for 20260711002000_return_stock_policy.sql
ALTER TABLE settings DROP COLUMN IF EXISTS returned_stock_to_active;
