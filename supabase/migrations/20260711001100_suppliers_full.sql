-- 20260711001100_suppliers_full.sql
-- Phase 2 / P2-M1 — Suppliers: full management fields.
--
-- Phase 1 (migration 0900) created a MINIMAL suppliers table (name + the
-- mandatory columns, "Q2 approved") plus the deferred FK from
-- medication_batches.supplier_id. Phase 2 extends that SAME table IN PLACE —
-- preserving the FK, every existing row, and all Phase-1 tests — with contact,
-- address, tax-registration, payment-terms, an active flag, and notes.
--
-- Every added column is nullable or defaulted, so pre-existing name-only rows
-- stay valid without backfill. Suppliers are GLOBAL (no branch_id) — CLAUDE.md
-- allows suppliers to be shared across branches.

ALTER TABLE suppliers
    ADD COLUMN contact_name        VARCHAR(255),
    ADD COLUMN phone               VARCHAR(32),
    ADD COLUMN email               VARCHAR(255),
    ADD COLUMN address             VARCHAR(500),
    ADD COLUMN tax_registration_no VARCHAR(50),
    ADD COLUMN payment_terms       VARCHAR(120),
    ADD COLUMN is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN notes               TEXT;

-- Active-supplier lookups ordered by name (management list + PO supplier picker).
CREATE INDEX idx_suppliers_active ON suppliers(name) WHERE NOT is_deleted AND is_active;
