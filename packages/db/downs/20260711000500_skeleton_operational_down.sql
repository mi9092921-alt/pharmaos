-- Down migration for 20260711000500_skeleton_operational.sql
-- Reverse-dependency order; indexes fall with their tables.

DROP TABLE IF EXISTS invoice_items;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS stock_movements;
DROP TABLE IF EXISTS medication_batches;
