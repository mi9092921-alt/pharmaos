-- Down migration for 20260711002200_payments_xor.sql
-- Restore the original (looser) OR constraint from migration 1700.
ALTER TABLE payments DROP CONSTRAINT chk_payment_source;
ALTER TABLE payments
    ADD CONSTRAINT chk_payment_source CHECK (invoice_id IS NOT NULL OR return_id IS NOT NULL);
