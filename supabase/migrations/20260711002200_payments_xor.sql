-- Tighten payments source to a true XOR (P2 review fix D3).
--
-- P2-M7 shipped chk_payment_source as an OR (invoice_id IS NOT NULL OR
-- return_id IS NOT NULL), which technically permits a row linked to BOTH a
-- sale and a return. Application code always passes exactly one, so no data
-- violates the stricter rule — this makes the invariant DB-enforced rather
-- than convention-only: a payment belongs to a sale XOR a refund, never both.

ALTER TABLE payments DROP CONSTRAINT chk_payment_source;
ALTER TABLE payments
    ADD CONSTRAINT chk_payment_source CHECK ((invoice_id IS NULL) <> (return_id IS NULL));
