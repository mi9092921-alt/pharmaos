-- Down migration for 20260711001100_suppliers_full.sql
-- Reverses the Phase-2 supplier columns, leaving the Phase-1 name-only table.
DROP INDEX IF EXISTS idx_suppliers_active;
ALTER TABLE suppliers
    DROP COLUMN IF EXISTS notes,
    DROP COLUMN IF EXISTS is_active,
    DROP COLUMN IF EXISTS payment_terms,
    DROP COLUMN IF EXISTS tax_registration_no,
    DROP COLUMN IF EXISTS address,
    DROP COLUMN IF EXISTS email,
    DROP COLUMN IF EXISTS phone,
    DROP COLUMN IF EXISTS contact_name;
