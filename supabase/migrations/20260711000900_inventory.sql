-- 20260711000900_inventory.sql
-- Phase 1 / M7 — inventory backbone: suppliers (minimal, Q2), branch_inventory
-- derived cache, and the deferred FK from batches to suppliers.
--
-- CLAUDE.md invariants:
--   * medication_batches remain the ONLY quantity truth.
--   * branch_inventory.cached_quantity is DERIVED — updated in the SAME
--     transaction as every batch movement, rebuilt periodically/at boot
--     (drift check). Never a source of truth.

-- 1) Minimal suppliers (Q2 approved): name-only + mandatory columns.
--    Full supplier management (contacts, terms, POs) is Phase 2.
CREATE TABLE suppliers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);
CREATE TRIGGER trg_suppliers_touch BEFORE UPDATE ON suppliers
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- Attach the FK deferred since migration 0500 (skeleton note).
ALTER TABLE medication_batches
    ADD CONSTRAINT fk_batches_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers(id);

-- 2) branch_inventory — derived stock cache per (branch, medication).
CREATE TABLE branch_inventory (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    cached_quantity DECIMAL(12,3) NOT NULL DEFAULT 0,    -- smallest unit
    min_stock_level DECIMAL(12,3) DEFAULT 0,
    max_stock_level DECIMAL(12,3),
    reorder_point   DECIMAL(12,3),
    shelf_location  VARCHAR(50),

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_inventory_branch_med UNIQUE (branch_id, medication_id)
);
CREATE TRIGGER trg_branch_inventory_touch BEFORE UPDATE ON branch_inventory
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE INDEX idx_inventory_branch ON branch_inventory(branch_id, medication_id);
