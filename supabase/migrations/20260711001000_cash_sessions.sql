-- 20260711001000_cash_sessions.sql
-- Phase 1 / M10 — cashier cash sessions + cash carry-through on invoices.
--
-- Design:
--   * ONE open session per cashier per branch (partial unique index) — the
--     drawer a cashier is accountable for. Sales by users WITHOUT an open
--     session stay legal (cash_session_id NULL): the permission matrix lets a
--     pharmacist sell (sales.create) without cashier.open_session.
--   * expected/counted/discrepancy are captured AT CLOSE and frozen on the row
--     (the Z-report of a shift must not drift when later data changes).
--   * invoices carry tendered/change so the customer-facing cash math is part
--     of the sale record (drawer math itself uses invoice totals: the drawer
--     nets +total on a cash sale — tendered in, change back out).

-- 1) cash_sessions
CREATE TABLE cash_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    cashier_id      UUID NOT NULL REFERENCES users(id),
    status          VARCHAR(10) NOT NULL DEFAULT 'open',
    opening_float   DECIMAL(12,2) NOT NULL DEFAULT 0,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    closed_by       UUID REFERENCES users(id),
    expected_cash   DECIMAL(12,2),
    counted_cash    DECIMAL(12,2),
    discrepancy     DECIMAL(12,2),
    closing_notes   TEXT,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_cash_session_status CHECK (status IN ('open', 'closed')),
    CONSTRAINT chk_cash_session_float CHECK (opening_float >= 0),
    CONSTRAINT chk_cash_session_closed_fields CHECK (
        status = 'open'
        OR (closed_at IS NOT NULL AND expected_cash IS NOT NULL AND counted_cash IS NOT NULL)
    )
);
CREATE TRIGGER trg_cash_sessions_touch BEFORE UPDATE ON cash_sessions
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- The accountability rule: a cashier holds at most ONE open drawer per branch.
CREATE UNIQUE INDEX uq_cash_sessions_open_per_cashier
    ON cash_sessions (branch_id, cashier_id)
    WHERE status = 'open' AND NOT is_deleted;

CREATE INDEX idx_cash_sessions_branch_opened ON cash_sessions (branch_id, opened_at);

-- 2) invoices — session linkage + customer cash carry-through.
ALTER TABLE invoices
    ADD COLUMN cash_session_id UUID REFERENCES cash_sessions(id),
    ADD COLUMN tendered_amount DECIMAL(12,2),
    ADD COLUMN change_amount   DECIMAL(12,2),
    ADD CONSTRAINT chk_invoice_change CHECK (change_amount IS NULL OR change_amount >= 0),
    ADD CONSTRAINT chk_invoice_tendered CHECK (tendered_amount IS NULL OR tendered_amount >= 0);

CREATE INDEX idx_invoices_cash_session ON invoices (cash_session_id);
