-- 20260711001200_purchase_orders.sql
-- Phase 2 / P2-M2 — Purchase orders: request -> approve -> receive.
--
-- Operational tables (branch_id NOT NULL + the mandatory columns). Receiving a
-- PO line REUSES the Phase-1 inventory receiving path (a medication_batches row
-- + a purchase_in stock_movement + the derived-cache delta, all in one tx),
-- linked back via stock_movements.reference_type='purchase_order' / reference_id.
--
-- Quantities are stored in the SMALLEST unit (tablet/unit), consistent with
-- medication_batches; packaging_id records the level the line was ordered at
-- (reference/display). unit_cost is the cost of one smallest unit.

CREATE TABLE purchase_orders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    supplier_id     UUID NOT NULL REFERENCES suppliers(id),
    po_number       VARCHAR(30) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'draft',
                    -- draft | pending_approval | approved | partially_received | received | cancelled
    order_date      DATE NOT NULL DEFAULT CURRENT_DATE,
    expected_date   DATE,
    currency_code   CHAR(3) NOT NULL REFERENCES currencies(code),
    subtotal        DECIMAL(12, 2) NOT NULL DEFAULT 0,
    tax_amount      DECIMAL(12, 2) NOT NULL DEFAULT 0,  -- input VAT: wired in the Phase-2 VAT milestone
    total           DECIMAL(12, 2) NOT NULL DEFAULT 0,
    notes           TEXT,
    approved_by     UUID REFERENCES users(id),
    approved_at     TIMESTAMPTZ,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_purchase_orders_number UNIQUE (branch_id, po_number),
    CONSTRAINT chk_po_status CHECK (
        status IN ('draft', 'pending_approval', 'approved', 'partially_received', 'received', 'cancelled')
    ),
    CONSTRAINT chk_po_amounts CHECK (subtotal >= 0 AND tax_amount >= 0 AND total >= 0)
);
CREATE TRIGGER trg_purchase_orders_touch BEFORE UPDATE ON purchase_orders
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE purchase_items (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id         UUID NOT NULL REFERENCES branches(id),
    purchase_order_id UUID NOT NULL REFERENCES purchase_orders(id),
    medication_id     UUID NOT NULL REFERENCES medications(id),
    packaging_id      UUID NOT NULL REFERENCES medication_packaging(id),  -- ordered level (reference)
    qty_ordered       DECIMAL(12, 3) NOT NULL,             -- smallest unit
    qty_received      DECIMAL(12, 3) NOT NULL DEFAULT 0,   -- smallest unit
    unit_cost         DECIMAL(12, 2) NOT NULL,             -- cost of one smallest unit
    line_total        DECIMAL(12, 2) NOT NULL,

    is_deleted        BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version      BIGINT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by        UUID REFERENCES users(id),
    updated_by        UUID REFERENCES users(id),

    CONSTRAINT chk_po_item_qty CHECK (qty_ordered > 0 AND qty_received >= 0 AND unit_cost >= 0)
);
CREATE TRIGGER trg_purchase_items_touch BEFORE UPDATE ON purchase_items
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE INDEX idx_purchase_orders_branch ON purchase_orders(branch_id, created_at DESC);
CREATE INDEX idx_purchase_orders_supplier ON purchase_orders(supplier_id) WHERE NOT is_deleted;
CREATE INDEX idx_purchase_items_po ON purchase_items(purchase_order_id) WHERE NOT is_deleted;
