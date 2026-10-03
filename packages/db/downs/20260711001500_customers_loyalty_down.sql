-- Down migration for 20260711001500_customers_loyalty.sql
-- Reverse order: drop the invoices FK column first, then the ledger, then customers.
DROP INDEX IF EXISTS idx_invoices_customer;
ALTER TABLE invoices DROP COLUMN IF EXISTS customer_id;
DROP TABLE IF EXISTS loyalty_transactions;
DROP TABLE IF EXISTS customers;
