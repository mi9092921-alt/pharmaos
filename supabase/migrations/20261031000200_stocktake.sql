-- 20261031000200_stocktake.sql
-- Stocktake/shortages round M2 — the stocktake module (الجرد).
--
-- Design (owner-approved plan):
--   * Count per BATCH (medication_batches is the only quantity truth — rule 15).
--     stocktake_lines snapshot system_qty_at_count UNDER LOCK at count-record
--     time, so concurrent sales never corrupt the diff; approval re-locks and
--     applies counted - snapshot via the shared adjustment primitive, writing
--     one 'adjustment' stock_movement per line with reference_type='stocktake'
--     (reference_type is a free VARCHAR — no CHECK to extend).
--   * Sessions: daily_quick (fastest movers / highest value / controlled /
--     cold-chain / past-variance) vs weekly_cycle (rotate shelf zones so the
--     week covers every shelf) vs custom. Blind count by default: the counter
--     never sees the recorded quantity until review (masked in the lines API,
--     the count response AND the CSV export — one serializer).
--   * A line whose live quantity + diff < 0 at approval is flagged
--     needs_recount and skipped — approval NEVER fails wholesale on one stale
--     line. Controlled-substance lines are never auto-settled implicitly: the
--     approver is manager-only (inventory.stocktake_approve) and every applied
--     controlled diff writes controlled_substance_log(source='stocktake').
--   * stocktake_settings: one row per branch — overdue days for the
--     stocktake_overdue alert, blind default for new sessions.

CREATE TABLE stocktake_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    session_type    VARCHAR(20) NOT NULL,               -- daily_quick | weekly_cycle | custom
    scope           JSONB NOT NULL DEFAULT '{}'::jsonb, -- zones / criteria snapshot for audit
    status          VARCHAR(15) NOT NULL DEFAULT 'draft',
    blind           BOOLEAN NOT NULL DEFAULT TRUE,
    started_by      UUID REFERENCES users(id),
    started_at      TIMESTAMPTZ,
    approved_by     UUID REFERENCES users(id),
    approved_at     TIMESTAMPTZ,
    lines_expected  INTEGER NOT NULL DEFAULT 0,
    lines_counted   INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_stocktake_session_type CHECK (
        session_type IN ('daily_quick', 'weekly_cycle', 'custom')
    ),
    CONSTRAINT chk_stocktake_session_status CHECK (
        status IN ('draft', 'counting', 'review', 'approved', 'cancelled')
    )
);
CREATE INDEX idx_stocktake_sessions_branch
    ON stocktake_sessions(branch_id, status, created_at DESC) WHERE NOT is_deleted;
CREATE TRIGGER trg_stocktake_sessions_touch BEFORE UPDATE ON stocktake_sessions
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE stocktake_lines (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              UUID NOT NULL REFERENCES stocktake_sessions(id),
    branch_id               UUID NOT NULL REFERENCES branches(id),
    batch_id                UUID NOT NULL REFERENCES medication_batches(id),
    medication_id           UUID NOT NULL REFERENCES medications(id),
    system_qty_at_count     DECIMAL(12,3),              -- snapshot UNDER LOCK when counted
    counted_qty             DECIMAL(12,3),              -- NULL until counted
    diff                    DECIMAL(12,3),              -- counted_qty - system_qty_at_count
    reason_code             VARCHAR(30),                -- miscount | breakage | theft | found | unknown
    note                    TEXT,
    counted_by              UUID REFERENCES users(id),
    counted_at              TIMESTAMPTZ,
    adjustment_movement_id  UUID REFERENCES stock_movements(id),
    flag                    VARCHAR(20),                -- needs_recount (live qty moved against the diff)

    is_deleted              BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version            BIGINT NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by              UUID REFERENCES users(id),
    updated_by              UUID REFERENCES users(id),

    CONSTRAINT uq_stocktake_line UNIQUE (session_id, batch_id)
);
CREATE INDEX idx_stocktake_lines_session
    ON stocktake_lines(session_id, id) WHERE NOT is_deleted;
CREATE TRIGGER trg_stocktake_lines_touch BEFORE UPDATE ON stocktake_lines
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE stocktake_settings (
    branch_id       UUID PRIMARY KEY REFERENCES branches(id),
    daily_overdue_days  INTEGER NOT NULL DEFAULT 7 CHECK (daily_overdue_days BETWEEN 1 AND 90),
    blind_default   BOOLEAN NOT NULL DEFAULT TRUE,

    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);
CREATE TRIGGER trg_stocktake_settings_touch BEFORE UPDATE ON stocktake_settings
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- Cold-chain flag (no fridge field existed — only free-text storage_conditions).
-- Backfilled below from common cold-chain phrasings; reviewed post-migration
-- via the catalog cold-chain-review CLI (keyword heuristics miss edge cases).
-- ============================================================================
ALTER TABLE medications
    ADD COLUMN is_cold_chain BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE medications SET is_cold_chain = TRUE
WHERE storage_conditions IS NOT NULL
  AND (
        storage_conditions ILIKE '%تبريد%'
     OR storage_conditions ILIKE '%ثلاجة%'
     OR storage_conditions ILIKE '%2-8%'
     OR storage_conditions ILIKE '%2–8%'
     OR storage_conditions ILIKE '%cold%'
     OR storage_conditions ILIKE '%refriger%'
  );

CREATE INDEX idx_medications_cold_chain ON medications(is_cold_chain)
    WHERE is_cold_chain AND NOT is_deleted;

-- ============================================================================
-- Controlled register extension (owner decision): stocktake diffs on
-- controlled substances must hit the SAME append-only register as sales.
-- invoice_id/invoice_item_id become nullable; a row is either a SALE row
-- (invoice required) or a STOCKTAKE row (session required). quantity_delta is
-- signed (shrinkage negative, found stock positive); sale rows keep the
-- positive quantity_dispensed unchanged.
-- ============================================================================
ALTER TABLE controlled_substance_log
    ALTER COLUMN invoice_id DROP NOT NULL,
    ALTER COLUMN invoice_item_id DROP NOT NULL,
    ADD COLUMN source VARCHAR(15) NOT NULL DEFAULT 'sale',
    ADD COLUMN stocktake_session_id UUID REFERENCES stocktake_sessions(id),
    ADD COLUMN quantity_delta DECIMAL(12,3);

-- quantity_dispensed keeps its SALE meaning (units dispensed, > 0). A
-- stocktake row records the signed movement in quantity_delta with
-- quantity_dispensed = 0, so the original strictly-positive CHECK is relaxed
-- to non-negative (sale rows are unchanged in practice).
ALTER TABLE controlled_substance_log DROP CONSTRAINT chk_controlled_qty;
ALTER TABLE controlled_substance_log
    ADD CONSTRAINT chk_controlled_qty CHECK (quantity_dispensed >= 0),
    ADD CONSTRAINT chk_controlled_source CHECK (
        (source = 'sale' AND invoice_id IS NOT NULL AND invoice_item_id IS NOT NULL
            AND quantity_delta IS NULL)
     OR (source = 'stocktake' AND stocktake_session_id IS NOT NULL
            AND quantity_delta IS NOT NULL AND quantity_dispensed = 0)
    );

CREATE INDEX idx_controlled_log_stocktake
    ON controlled_substance_log(stocktake_session_id) WHERE source = 'stocktake';
