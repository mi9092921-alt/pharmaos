-- 20261031000300_shortage_items.sql
-- Stocktake/shortages round M4 — the shortages module (النواقص).
--
-- Two kinds, per the owner's design:
--   pharmacy — the branch's own stockouts/low items; auto-fed from the alert
--     engine's out_of_stock/low_stock findings (sync_auto, idempotent) and
--     from manual entry / the inventory page button.
--   market   — items customers ask for that the branch (or its suppliers)
--     cannot provide; the POS "customer asked" button feeds them. ask_count
--     quantifies the lost sales signal.
--
-- Anti-resurrection (owner point 2): auto-sync must never re-add an item the
-- staff closed. Closed rows carry suppress_auto_resync (set on manual
-- cancel/unavailable, cleared when stock actually arrives and passes the
-- threshold — "recovered"): the next sync re-adds only after a real recovery.
--
-- The partial unique indexes make concurrent sync/creation safe (ON CONFLICT)
-- and duplicate-free while open/ordered.

CREATE TABLE shortage_items (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id          UUID NOT NULL REFERENCES branches(id),
    kind               VARCHAR(10) NOT NULL,            -- pharmacy | market
    source             VARCHAR(20) NOT NULL,            -- auto_out_of_stock | auto_low_stock | manual | customer_request
    medication_id      UUID REFERENCES medications(id), -- NULL = not in the catalog (market item by name)
    name_text          VARCHAR(255) NOT NULL,           -- display name (catalog name or free text)
    qty_needed         DECIMAL(12,3) NOT NULL DEFAULT 1,
    status             VARCHAR(15) NOT NULL DEFAULT 'open',
    supplier_id        UUID REFERENCES suppliers(id),
    purchase_order_id  UUID REFERENCES purchase_orders(id),
    customer_name      VARCHAR(255),                    -- who asked (call them when it arrives)
    customer_phone     VARCHAR(30),
    ask_count          INTEGER NOT NULL DEFAULT 1,      -- how many customers asked (market)
    last_asked_at      TIMESTAMPTZ,
    ordered_at         TIMESTAMPTZ,
    received_at        TIMESTAMPTZ,
    last_checked_at    TIMESTAMPTZ,                     -- supplier said "unavailable" — recheck later
    suppress_auto_resync BOOLEAN NOT NULL DEFAULT FALSE,
    notes              TEXT,

    is_deleted         BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version       BIGINT NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by         UUID REFERENCES users(id),
    updated_by         UUID REFERENCES users(id),

    CONSTRAINT chk_shortage_kind CHECK (kind IN ('pharmacy', 'market')),
    CONSTRAINT chk_shortage_source CHECK (
        source IN ('auto_out_of_stock', 'auto_low_stock', 'manual', 'customer_request')
    ),
    CONSTRAINT chk_shortage_status CHECK (
        status IN ('open', 'ordered', 'received', 'unavailable', 'cancelled')
    ),
    CONSTRAINT chk_shortage_qty CHECK (qty_needed > 0),
    CONSTRAINT chk_shortage_ask CHECK (ask_count >= 1)
);
CREATE TRIGGER trg_shortage_items_touch BEFORE UPDATE ON shortage_items
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- one live row per (branch, catalog item, kind)
CREATE UNIQUE INDEX uq_shortage_open_med
    ON shortage_items(branch_id, medication_id, kind)
    WHERE medication_id IS NOT NULL AND status IN ('open', 'ordered') AND NOT is_deleted;
-- free-text market items: one live row per (branch, name, kind)
CREATE UNIQUE INDEX uq_shortage_open_name
    ON shortage_items(branch_id, kind, LOWER(name_text))
    WHERE medication_id IS NULL AND status IN ('open', 'ordered') AND NOT is_deleted;

-- list screens: newest live rows per kind/status first
CREATE INDEX idx_shortage_branch_kind
    ON shortage_items(branch_id, kind, status, updated_at DESC) WHERE NOT is_deleted;
