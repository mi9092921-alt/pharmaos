-- Down migration for 20260711001600_vat_tax.sql
ALTER TABLE invoice_items DROP COLUMN IF EXISTS tax_amount;
ALTER TABLE invoice_items DROP COLUMN IF EXISTS tax_rate;
ALTER TABLE medications DROP COLUMN IF EXISTS is_medicine;
