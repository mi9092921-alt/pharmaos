-- 20260711000700_settings.sql
-- Phase 1 / M4 — per-branch settings: invoice-template fields (CLAUDE.md
-- InvoiceTemplate) + POS options. Operational (branch-scoped): one row per
-- branch (UNIQUE branch_id). Multi-branch (Phase 4) reuses this as-is.
--
-- `phone`, `tax_registration_no`, `license_number` here are the PHARMACY's
-- public identity printed on receipts — NOT personal data, so not encrypted
-- (unlike users.phone). max_discount_percent backs the POS discount cap (Q4).

CREATE TABLE settings (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id                 UUID NOT NULL REFERENCES branches(id),

    -- Invoice template (CLAUDE.md InvoiceTemplate)
    pharmacy_name             VARCHAR(255) NOT NULL,
    pharmacy_logo             VARCHAR(500),              -- logo URL/path (optional)
    license_number            VARCHAR(100),              -- رقم الترخيص
    address                   VARCHAR(500),
    phone                     VARCHAR(32),
    tax_registration_no       VARCHAR(50),               -- إلزامي مع ETA (Phase 2)
    return_policy             TEXT,
    thank_you_message         VARCHAR(255),
    paper_size                VARCHAR(8) NOT NULL DEFAULT '80mm',
    show_pharmacist_signature BOOLEAN NOT NULL DEFAULT FALSE,
    show_qr_code              BOOLEAN NOT NULL DEFAULT FALSE,   -- إلزامي للإيصال ETA

    -- POS options
    max_discount_percent      DECIMAL(5,2) NOT NULL DEFAULT 0,  -- branch discount cap (Q4)

    is_deleted                BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version              BIGINT NOT NULL DEFAULT 0,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by                UUID REFERENCES users(id),
    updated_by                UUID REFERENCES users(id),

    CONSTRAINT uq_settings_branch UNIQUE (branch_id),
    CONSTRAINT chk_settings_paper CHECK (paper_size IN ('80mm', 'A4', 'A5')),
    CONSTRAINT chk_settings_discount CHECK (max_discount_percent >= 0 AND max_discount_percent <= 100)
);
CREATE TRIGGER trg_settings_touch BEFORE UPDATE ON settings
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE INDEX idx_settings_branch ON settings(branch_id) WHERE NOT is_deleted;
