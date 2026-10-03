-- Expenses tracking + categories (P2-M9 — المصروفات وتصنيفاتها).
--
-- CLAUDE.md groups {cash_sessions, expenses, expense_categories} under
-- "Financial" tables (line ~380) but only explicitly classifies branch_id
-- placement for the two broader table buckets (Catalog = global, Operational
-- = branch_id NOT NULL) — expense_categories itself isn't named in either
-- list. Design decision (documented, mirrors `categories` for medications):
--   - expense_categories = GLOBAL/catalog-style lookup (no branch_id). A
--     pharmacy wants ONE shared chart of expense categories (rent, utilities,
--     salaries, maintenance, ...) across all its branches for consistent
--     roll-up reporting — not a re-typed list per branch. Same shape as the
--     `categories` table (20260711000300_catalog_schema.sql): name_ar NOT
--     NULL, name_en optional, mandatory columns, touch trigger.
--   - expenses = OPERATIONAL (branch_id NOT NULL) — each expense is incurred
--     and recorded at a specific branch, same as invoices/payments/etc.
--
-- Permission finance.expenses already seeded (super_admin, branch_manager;
-- packages/shared/src/permissions.ts, transcribed from CLAUDE.md) — a SINGLE
-- permission covers view+create+edit+delete for this domain (unlike
-- prescriptions.*, CLAUDE.md does not split this one), so no RBAC seed change
-- accompanies this migration.
--
-- recorded_by (named in the execution plan's prose) is NOT a separate column
-- here — it is exactly MandatoryColumnsMixin's created_by (who wrote the row);
-- adding a second actor column would be a redundant duplicate.

CREATE TABLE expense_categories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name_ar         VARCHAR(120) NOT NULL,
    name_en         VARCHAR(120),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);
CREATE TRIGGER trg_expense_categories_touch BEFORE UPDATE ON expense_categories
    FOR EACH ROW EXECUTE FUNCTION touch_row();
CREATE INDEX idx_expense_categories_active ON expense_categories(name_ar)
    WHERE NOT is_deleted AND is_active;

CREATE TABLE expenses (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id            UUID NOT NULL REFERENCES branches(id),
    expense_category_id  UUID NOT NULL REFERENCES expense_categories(id),
    amount               DECIMAL(12, 2) NOT NULL,
    currency_code        CHAR(3) NOT NULL REFERENCES currencies(code),
    expense_date         DATE NOT NULL,
    description          VARCHAR(500),
    payment_method       VARCHAR(20) NOT NULL DEFAULT 'cash',

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_expenses_amount_positive CHECK (amount > 0),
    CONSTRAINT chk_expenses_payment_method
        CHECK (payment_method IN ('cash', 'card', 'bank_transfer'))
);
CREATE TRIGGER trg_expenses_touch BEFORE UPDATE ON expenses
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- Hot paths: a branch's expense list/report scoped to a date range (default
-- listing order is newest-first) and per-category filtering/roll-ups.
CREATE INDEX idx_expenses_branch_date ON expenses(branch_id, expense_date DESC)
    WHERE NOT is_deleted;
CREATE INDEX idx_expenses_category ON expenses(expense_category_id)
    WHERE NOT is_deleted;
