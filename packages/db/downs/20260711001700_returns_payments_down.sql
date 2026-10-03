-- Down migration for 20260711001700_returns_payments.sql
-- Reverse order: payments (FKs returns/invoices) first, then return_items, then returns.
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS return_items;
DROP TABLE IF EXISTS returns;
