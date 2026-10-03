-- 20260711001300_pack_serials.sql
-- Phase 2 / P2-M3 — per-pack serials (EDA track & trace, decrees 161/475/2025).
--
-- Every 2D (GS1 DataMatrix) pack carries GTIN + expiry + batch + a random
-- serial. We persist the serial on RECEIVE (linked to its batch) and mark it
-- DISPENSED (linked to its invoice) on sale — the trail the national-reporting
-- module (P2-M11, tt_events) will submit. Operational table (branch_id + the
-- mandatory columns). UNIQUE(gtin, serial_number): a duplicate serial is a
-- "non-compliant product" (decree 804) and is rejected at capture (E-TT-002).

CREATE TABLE pack_serials (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id            UUID NOT NULL REFERENCES branches(id),
    batch_id             UUID NOT NULL REFERENCES medication_batches(id),
    serial_number        VARCHAR(64) NOT NULL,       -- random serial from the 2D code
    gtin                 VARCHAR(14) NOT NULL,
    status               VARCHAR(20) NOT NULL DEFAULT 'in_stock',
                         -- in_stock | dispensed | returned | quarantined | reported_destroyed
    dispensed_invoice_id UUID REFERENCES invoices(id),
    tt_report_status     VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending | reported | failed

    is_deleted           BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version         BIGINT NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by           UUID REFERENCES users(id),
    updated_by           UUID REFERENCES users(id),

    CONSTRAINT uq_pack_serials_gtin_serial UNIQUE (gtin, serial_number),
    CONSTRAINT chk_pack_serial_status CHECK (
        status IN ('in_stock', 'dispensed', 'returned', 'quarantined', 'reported_destroyed')
    ),
    CONSTRAINT chk_pack_serial_tt CHECK (tt_report_status IN ('pending', 'reported', 'failed'))
);
CREATE TRIGGER trg_pack_serials_touch BEFORE UPDATE ON pack_serials
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- (gtin, serial_number) lookups are served by the UNIQUE constraint's index.
CREATE INDEX idx_pack_serials_batch ON pack_serials(batch_id) WHERE NOT is_deleted;
-- National-reporting outbox scan (P2-M11): unreported events per branch.
CREATE INDEX idx_pack_serials_report ON pack_serials(branch_id, tt_report_status)
    WHERE tt_report_status = 'pending';
