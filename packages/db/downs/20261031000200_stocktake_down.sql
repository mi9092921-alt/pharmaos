-- 20261031000200_stocktake_down.sql
-- Paired down for the stocktake module.
--
-- controlled_substance_log is APPEND-ONLY (trigger + REVOKE): the rollback
-- must NEVER delete stocktake entries. If any exist, the down REFUSES loudly
-- — restoring NOT NULL on invoice_id with such rows present would also fail,
-- and silently wiping regulatory records is not an option. Resolution: export
-- the register first, then purge the stocktake source rows via a deliberate
-- DBA action (the immutability trigger must be dropped first — a conscious,
-- logged step, not a silent side effect of a migration rollback).

DO $$
DECLARE
    stocktake_rows BIGINT;
BEGIN
    SELECT COUNT(*) INTO stocktake_rows
    FROM controlled_substance_log WHERE source = 'stocktake';
    IF stocktake_rows > 0 THEN
        RAISE EXCEPTION
            'cannot roll back 20261031000200: % controlled_substance_log row(s) with source=''stocktake'' exist — the register is append-only; export and purge them deliberately first',
            stocktake_rows;
    END IF;
END $$;

DROP INDEX IF EXISTS idx_controlled_log_stocktake;
ALTER TABLE controlled_substance_log DROP CONSTRAINT IF EXISTS chk_controlled_source;
ALTER TABLE controlled_substance_log DROP CONSTRAINT IF EXISTS chk_controlled_qty;
ALTER TABLE controlled_substance_log
    ADD CONSTRAINT chk_controlled_qty CHECK (quantity_dispensed > 0);
ALTER TABLE controlled_substance_log
    DROP COLUMN IF EXISTS quantity_delta,
    DROP COLUMN IF EXISTS stocktake_session_id,
    DROP COLUMN IF EXISTS source;
ALTER TABLE controlled_substance_log
    ALTER COLUMN invoice_id SET NOT NULL,
    ALTER COLUMN invoice_item_id SET NOT NULL;

DROP INDEX IF EXISTS idx_medications_cold_chain;
ALTER TABLE medications DROP COLUMN IF EXISTS is_cold_chain;

DROP TABLE IF EXISTS stocktake_settings;
DROP TABLE IF EXISTS stocktake_lines;
DROP TABLE IF EXISTS stocktake_sessions;
