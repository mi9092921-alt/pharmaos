-- 20260711001500_customers_loyalty.sql
-- Phase 2 / P2-M5 — customer management + minimal loyalty.
--
-- CLAUDE.md encrypted-field classification: national_id + insurance_number are
-- AES-256-GCM field-encrypted (service layer) -> BYTEA; name + phone stay
-- PLAINTEXT because they are the pharmacy's primary search/lookup keys.
-- Customers are GLOBAL (shared across branches per CLAUDE.md §sync scope), so
-- no branch_id here; the loyalty balance is per customer.
--
-- Loyalty is ledger-first: loyalty_transactions is the append-only truth and
-- customers.loyalty_points is the derived running balance (maintained in the
-- same transaction as each ledger row — same discipline as medication_batches
-- vs branch_inventory). A recompute helper re-derives the balance from the ledger.

CREATE TABLE customers (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                        VARCHAR(160) NOT NULL,   -- plaintext (primary search key)
    phone                       VARCHAR(32),             -- plaintext (primary lookup key)
    national_id_encrypted       BYTEA,                   -- AES-256-GCM (service layer)
    insurance_number_encrypted  BYTEA,                   -- AES-256-GCM (service layer)
    loyalty_points              BIGINT NOT NULL DEFAULT 0,  -- derived balance (ledger is truth)
    notes                       TEXT,
    is_active                   BOOLEAN NOT NULL DEFAULT TRUE,

    is_deleted                  BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version                BIGINT NOT NULL DEFAULT 0,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by                  UUID REFERENCES users(id),
    updated_by                  UUID REFERENCES users(id),

    CONSTRAINT chk_customer_points CHECK (loyalty_points >= 0)
);
CREATE TRIGGER trg_customers_touch BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- Arabic-name search (trigram fallback, mirrors idx_medications_trgm) + phone lookup.
CREATE INDEX idx_customers_name_trgm ON customers
    USING GIN (normalize_arabic(name) gin_trgm_ops);
CREATE INDEX idx_customers_phone ON customers(phone)
    WHERE phone IS NOT NULL AND NOT is_deleted;

-- Append-only loyalty ledger. earn (auto on sale) | redeem | adjust (manual).
CREATE TABLE loyalty_transactions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id    UUID NOT NULL REFERENCES customers(id),
    points_delta   BIGINT NOT NULL,             -- signed: earn (+), redeem/adjust (±)
    txn_type       VARCHAR(20) NOT NULL,        -- earn | redeem | adjust
    reference_type VARCHAR(30),                 -- invoice | manual
    reference_id   UUID,
    reason         TEXT,

    is_deleted     BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version   BIGINT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by     UUID REFERENCES users(id),
    updated_by     UUID REFERENCES users(id),

    CONSTRAINT chk_loyalty_txn_type CHECK (txn_type IN ('earn', 'redeem', 'adjust'))
);
CREATE TRIGGER trg_loyalty_transactions_touch BEFORE UPDATE ON loyalty_transactions
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- Ledger scan per customer, newest first (history + recompute).
CREATE INDEX idx_loyalty_customer ON loyalty_transactions(customer_id, created_at DESC);

-- Attach a customer to a sale (nullable — walk-in sales carry no customer).
-- The data foundation for loyalty accrual and customer purchase history.
ALTER TABLE invoices ADD COLUMN customer_id UUID REFERENCES customers(id);
CREATE INDEX idx_invoices_customer ON invoices(customer_id) WHERE customer_id IS NOT NULL;
