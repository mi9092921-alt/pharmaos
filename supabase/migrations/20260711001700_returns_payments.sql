-- 20260711001700_returns_payments.sql
-- Phase 2 / P2-M7 — returns (credit notes) + the payments money ledger.
--
-- CLAUDE.md rule 14: a completed invoice is NEVER modified — a return is a
-- separate credit note (returns + return_items) that references the original
-- invoice and puts stock back via return_in movements. payments is the signed
-- money ledger: +amount for a sale receipt, -amount for a refund; each payment
-- belongs to EITHER an invoice (sale) or a return (refund).

CREATE TABLE returns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id           UUID NOT NULL REFERENCES branches(id),
    original_invoice_id UUID NOT NULL REFERENCES invoices(id),
    return_number       VARCHAR(30) NOT NULL,        -- RET-YYYYMMDD-NNNN per branch
    reason              TEXT,
    currency_code       CHAR(3) NOT NULL,
    subtotal            DECIMAL(12,2) NOT NULL,      -- net credit (magnitude)
    tax_amount          DECIMAL(12,2) NOT NULL DEFAULT 0,
    total               DECIMAL(12,2) NOT NULL,      -- credited to the customer (magnitude)
    refund_method       VARCHAR(20) NOT NULL DEFAULT 'cash',  -- cash | card | store_credit
    customer_id         UUID REFERENCES customers(id),
    cash_session_id     UUID REFERENCES cash_sessions(id),

    is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version        BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID REFERENCES users(id),
    updated_by          UUID REFERENCES users(id),

    CONSTRAINT uq_returns_number UNIQUE (branch_id, return_number),
    CONSTRAINT chk_return_refund_method CHECK (refund_method IN ('cash', 'card', 'store_credit'))
);
CREATE TRIGGER trg_returns_touch BEFORE UPDATE ON returns
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE return_items (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id         UUID NOT NULL REFERENCES branches(id),
    return_id         UUID NOT NULL REFERENCES returns(id),
    invoice_item_id   UUID REFERENCES invoice_items(id),  -- the original sold line
    medication_id     UUID NOT NULL REFERENCES medications(id),
    packaging_id      UUID NOT NULL REFERENCES medication_packaging(id),
    batch_id          UUID NOT NULL REFERENCES medication_batches(id),  -- stock returns here
    quantity          DECIMAL(10,3) NOT NULL,       -- at the sold packaging level
    qty_smallest      DECIMAL(12,3) NOT NULL,       -- smallest unit (stock delta)
    unit_price        DECIMAL(12,2) NOT NULL,
    line_total        DECIMAL(12,2) NOT NULL,
    tax_rate          DECIMAL(5,2) NOT NULL DEFAULT 0,
    tax_amount        DECIMAL(12,2) NOT NULL DEFAULT 0,

    is_deleted        BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version      BIGINT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by        UUID REFERENCES users(id),
    updated_by        UUID REFERENCES users(id)
);
CREATE TRIGGER trg_return_items_touch BEFORE UPDATE ON return_items
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE payments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    invoice_id      UUID REFERENCES invoices(id),   -- sale receipt (+)
    return_id       UUID REFERENCES returns(id),    -- refund (-)
    amount          DECIMAL(12,2) NOT NULL,         -- signed: + sale, - refund
    method          VARCHAR(20) NOT NULL,           -- cash | card | store_credit
    cash_session_id UUID REFERENCES cash_sessions(id),
    reference       VARCHAR(64),

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_payment_method CHECK (method IN ('cash', 'card', 'store_credit')),
    CONSTRAINT chk_payment_source CHECK (invoice_id IS NOT NULL OR return_id IS NOT NULL)
);
CREATE TRIGGER trg_payments_touch BEFORE UPDATE ON payments
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- A credit note lists returns for an invoice; over-return checks scan by line.
CREATE INDEX idx_returns_invoice ON returns(original_invoice_id) WHERE NOT is_deleted;
CREATE INDEX idx_returns_branch ON returns(branch_id, created_at DESC) WHERE NOT is_deleted;
CREATE INDEX idx_return_items_return ON return_items(return_id) WHERE NOT is_deleted;
CREATE INDEX idx_return_items_invoice_item ON return_items(invoice_item_id)
    WHERE invoice_item_id IS NOT NULL AND NOT is_deleted;
-- Money ledger scans per sale / per refund.
CREATE INDEX idx_payments_invoice ON payments(invoice_id) WHERE invoice_id IS NOT NULL;
CREATE INDEX idx_payments_return ON payments(return_id) WHERE return_id IS NOT NULL;
