-- Down migration for 20260711001000_cash_sessions.sql
DROP INDEX IF EXISTS idx_invoices_cash_session;
ALTER TABLE invoices
    DROP CONSTRAINT IF EXISTS chk_invoice_tendered,
    DROP CONSTRAINT IF EXISTS chk_invoice_change,
    DROP COLUMN IF EXISTS change_amount,
    DROP COLUMN IF EXISTS tendered_amount,
    DROP COLUMN IF EXISTS cash_session_id;
DROP TABLE IF EXISTS cash_sessions;
