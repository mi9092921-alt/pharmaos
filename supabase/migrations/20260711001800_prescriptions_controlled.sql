-- 20260711001800_prescriptions_controlled.sql
-- Phase 2 / P2-M8 — Prescriptions management + the controlled-substance register.
--
-- prescriptions / prescription_items are ordinary operational tables (mandatory
-- columns). notes is AES-256-GCM field-encrypted (service layer) per CLAUDE.md's
-- explicit classification ("prescriptions.notes — ملاحظات طبية حساسة") -> BYTEA.
--
-- controlled_substance_log is a SEPARATE, append-only register (CLAUDE.md Data
-- Security Rules: "حماية Controlled Substances: سجل منفصل + إشعارات + لا حذف
-- نهائياً" — a dedicated log, notifications, NEVER deleted). It is deliberately
-- exempt from the mandatory-column contract (like audit_logs): no updated_at/
-- updated_by/is_deleted/sync_version, and a DB-level trigger forbids UPDATE and
-- DELETE for every role (the strongest, role-independent enforcement of "never
-- delete" — the same mechanism already proven on audit_logs).

CREATE TABLE prescriptions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id          UUID NOT NULL REFERENCES branches(id),
    customer_id        UUID REFERENCES customers(id),   -- nullable: walk-in, no customer record
    doctor_name        VARCHAR(160) NOT NULL,
    doctor_license_no  VARCHAR(50),
    prescription_date  DATE NOT NULL,
    notes_encrypted    BYTEA,                            -- AES-256-GCM (service layer)
    status             VARCHAR(20) NOT NULL DEFAULT 'pending',
                       -- pending | partially_fulfilled | fulfilled | expired | cancelled

    is_deleted         BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version       BIGINT NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by         UUID REFERENCES users(id),
    updated_by         UUID REFERENCES users(id),

    CONSTRAINT chk_prescription_status CHECK (
        status IN ('pending', 'partially_fulfilled', 'fulfilled', 'expired', 'cancelled')
    )
);
CREATE TRIGGER trg_prescriptions_touch BEFORE UPDATE ON prescriptions
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE prescription_items (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id                UUID NOT NULL REFERENCES branches(id),
    prescription_id          UUID NOT NULL REFERENCES prescriptions(id),
    medication_id            UUID NOT NULL REFERENCES medications(id),
    packaging_id             UUID NOT NULL REFERENCES medication_packaging(id),
    prescribed_qty           DECIMAL(10,3) NOT NULL,      -- at packaging_id level
    prescribed_qty_smallest  DECIMAL(12,3) NOT NULL,      -- snapshot conversion (smallest unit)
    dispensed_qty_smallest   DECIMAL(12,3) NOT NULL DEFAULT 0,  -- running total dispensed so far

    is_deleted               BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version             BIGINT NOT NULL DEFAULT 0,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by               UUID REFERENCES users(id),
    updated_by                UUID REFERENCES users(id),

    CONSTRAINT chk_prescription_item_qty CHECK (prescribed_qty > 0),
    CONSTRAINT chk_prescription_item_dispensed CHECK (
        dispensed_qty_smallest >= 0 AND dispensed_qty_smallest <= prescribed_qty_smallest
    )
);
CREATE TRIGGER trg_prescription_items_touch BEFORE UPDATE ON prescription_items
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE controlled_substance_log (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id          UUID NOT NULL REFERENCES branches(id),
    medication_id      UUID NOT NULL REFERENCES medications(id),
    batch_id           UUID NOT NULL REFERENCES medication_batches(id),
    invoice_id         UUID NOT NULL REFERENCES invoices(id),
    invoice_item_id    UUID NOT NULL REFERENCES invoice_items(id),
    prescription_id    UUID REFERENCES prescriptions(id),  -- nullable: not every controlled item is Rx-linked in the catalog
    quantity_dispensed DECIMAL(12,3) NOT NULL,              -- smallest unit
    dispensed_by       UUID NOT NULL REFERENCES users(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_controlled_qty CHECK (quantity_dispensed > 0)
);

CREATE INDEX idx_prescriptions_branch ON prescriptions(branch_id, created_at DESC)
    WHERE NOT is_deleted;
CREATE INDEX idx_prescriptions_customer ON prescriptions(customer_id)
    WHERE customer_id IS NOT NULL AND NOT is_deleted;
CREATE INDEX idx_prescription_items_prescription ON prescription_items(prescription_id)
    WHERE NOT is_deleted;
CREATE INDEX idx_controlled_log_medication ON controlled_substance_log(medication_id, created_at DESC);
CREATE INDEX idx_controlled_log_branch ON controlled_substance_log(branch_id, created_at DESC);
CREATE INDEX idx_controlled_log_invoice ON controlled_substance_log(invoice_id);

-- ============================================================================
-- Append-only enforcement at the DATABASE level (mirrors audit_logs exactly —
-- CLAUDE.md: controlled-substance records are never truly deleted, fires for
-- EVERY role including owner/superuser).
-- ============================================================================
CREATE OR REPLACE FUNCTION forbid_controlled_substance_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'controlled_substance_log is append-only';
END;
$$;

CREATE TRIGGER trg_controlled_substance_immutable
    BEFORE UPDATE OR DELETE ON controlled_substance_log
    FOR EACH ROW EXECUTE FUNCTION forbid_controlled_substance_mutation();

-- Defense-in-depth privilege revoke (app_user role already created by the
-- audit_log migration; CLAUDE.md — least-privilege application role).
REVOKE UPDATE, DELETE ON controlled_substance_log FROM app_user;
