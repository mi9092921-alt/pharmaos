-- 20260711000500_skeleton_operational.sql
-- Phase 0 / M12 — Walking-skeleton operational tables (CLAUDE.md Phase 0:
-- "Walking Skeleton: مسح → فاتورة → طباعة → حفظ offline").
--
-- The thin vertical slice needs exactly: medication_batches (quantity truth),
-- stock_movements (append-only ledger), invoices + invoice_items (the sale).
-- All are OPERATIONAL tables: branch_id NOT NULL + the mandatory columns.
--
-- Inventory rules enforced by this schema + the sales service:
--   * batches are the ONLY quantity truth (no quantity on medications — rule 15)
--   * every quantity change goes through stock_movements (rule 16)
--   * quantities are stored in the SMALLEST unit (tablet/unit)
--   * FEFO dispensing; no sale from status != 'active' (rule 18)
--
-- Notes:
--   * supplier_id has no FK yet — the suppliers table arrives with the
--     purchasing module (Phase 1/2 migration adds the constraint).
--   * branch_inventory (derived cache) is Phase 1 scope; the skeleton reads
--     truth directly from batches.

-- ============================================================================
-- 1) medication_batches — single source of truth for stock quantity.
-- ============================================================================
CREATE TABLE medication_batches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    batch_number    VARCHAR(50) NOT NULL,
    expiry_date     DATE NOT NULL,
    quantity        DECIMAL(12,3) NOT NULL DEFAULT 0,   -- smallest unit (tablet/unit)
    purchase_price  DECIMAL(12,2) NOT NULL,             -- purchase price of the smallest unit
    supplier_id     UUID,                               -- FK added with the suppliers table
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_batch_qty CHECK (quantity >= 0),
    CONSTRAINT chk_batch_status CHECK (
        status IN ('active', 'quarantined', 'expired', 'recalled', 'depleted')
    )
);
CREATE TRIGGER trg_medication_batches_touch BEFORE UPDATE ON medication_batches
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 2) stock_movements — append-only audit trail for every quantity change.
-- ============================================================================
CREATE TABLE stock_movements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    batch_id        UUID NOT NULL REFERENCES medication_batches(id),
    movement_type   VARCHAR(30) NOT NULL,
    quantity_delta  DECIMAL(12,3) NOT NULL,             -- signed, smallest unit
    reference_type  VARCHAR(30),                        -- invoice | purchase_order | return | manual
    reference_id    UUID,
    reason          TEXT,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_movement_type CHECK (
        movement_type IN (
            'purchase_in', 'sale_out', 'return_in', 'return_out',
            'adjustment', 'quarantine', 'expiry_writeoff', 'transfer_in', 'transfer_out'
        )
    )
);
CREATE TRIGGER trg_stock_movements_touch BEFORE UPDATE ON stock_movements
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 3) invoices — the completed invoice is immutable (changes go through credit
--    notes — forbidden action #14). Currency + amounts are SNAPSHOTS (DECIMAL).
-- ============================================================================
CREATE TABLE invoices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    invoice_number  VARCHAR(30) NOT NULL,
    invoice_type    VARCHAR(20) NOT NULL DEFAULT 'retail',
    status          VARCHAR(20) NOT NULL DEFAULT 'completed',
    currency_code   CHAR(3) NOT NULL REFERENCES currencies(code),
    subtotal        DECIMAL(12,2) NOT NULL,
    discount_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    tax_amount      DECIMAL(12,2) NOT NULL DEFAULT 0,   -- tax profile application: Phase 2
    total           DECIMAL(12,2) NOT NULL,
    payment_method  VARCHAR(20) NOT NULL DEFAULT 'cash',

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_invoices_number UNIQUE (branch_id, invoice_number),
    CONSTRAINT chk_invoice_type CHECK (
        invoice_type IN ('retail', 'wholesale', 'prescription', 'return')
    ),
    CONSTRAINT chk_invoice_status CHECK (status IN ('completed', 'cancelled')),
    CONSTRAINT chk_invoice_amounts CHECK (subtotal >= 0 AND total >= 0)
);
CREATE TRIGGER trg_invoices_touch BEFORE UPDATE ON invoices
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 4) invoice_items — one row per BATCH SLICE (FEFO may split a sold line
--    across batches; batch_id links each slice for tracking — CLAUDE.md:
--    "invoice_items تربط بـ batch_id للتتبع وFEFO").
-- ============================================================================
CREATE TABLE invoice_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    invoice_id      UUID NOT NULL REFERENCES invoices(id),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    packaging_id    UUID NOT NULL REFERENCES medication_packaging(id),  -- sold level
    batch_id        UUID NOT NULL REFERENCES medication_batches(id),
    quantity        DECIMAL(10,3) NOT NULL,             -- quantity at the SOLD level (display)
    qty_smallest    DECIMAL(12,3) NOT NULL,             -- deducted from the batch (smallest unit)
    unit_price      DECIMAL(12,2) NOT NULL,             -- selling price of the sold level (snapshot)
    line_total      DECIMAL(12,2) NOT NULL,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_item_qty CHECK (quantity > 0 AND qty_smallest > 0)
);
CREATE TRIGGER trg_invoice_items_touch BEFORE UPDATE ON invoice_items
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 5) Indexes (CLAUDE.md performance rules — plain CREATE INDEX on creation).
-- ============================================================================
CREATE INDEX idx_batches_branch_med ON medication_batches(branch_id, medication_id)
    WHERE NOT is_deleted AND status = 'active';
CREATE INDEX idx_batches_expiry ON medication_batches(branch_id, expiry_date)
    WHERE status = 'active';
CREATE INDEX idx_invoices_date ON invoices(created_at DESC, branch_id);
CREATE INDEX idx_invoice_items_invoice ON invoice_items(invoice_id);
CREATE INDEX idx_movements_batch ON stock_movements(batch_id) WHERE NOT is_deleted;
