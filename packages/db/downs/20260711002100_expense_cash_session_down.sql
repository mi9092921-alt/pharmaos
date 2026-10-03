-- Down migration for 20260711002100_expense_cash_session.sql
DROP INDEX IF EXISTS idx_expenses_session;
ALTER TABLE expenses DROP COLUMN IF EXISTS cash_session_id;
