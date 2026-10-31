-- 20261031000100_inventory_is_tracked.sql
-- Stocktake/shortages round M2 — F4: per-item stock tracking opt-out.
--
-- out_of_stock used to fire a CRITICAL alert for EVERY zero-quantity cache row:
-- discontinued items, stopped-trading lines, or "known but never received"
-- catalog rows (update_stock_settings creates cache rows with quantity 0) all
-- spammed the alert board with no way to silence them. is_tracked is the
-- per-item switch (default TRUE — zero behavior change on upgrade): an
-- untracked item is excluded from the out_of_stock / low_stock alert rules.
-- The batch quantities themselves are untouched (rule 15: batches stay the
-- sole quantity truth; this is a branch_inventory metadata flag only).

ALTER TABLE branch_inventory
    ADD COLUMN is_tracked BOOLEAN NOT NULL DEFAULT TRUE;

-- Settings screen: flipping tracking is a per-row read for the stock list.
CREATE INDEX idx_branch_inventory_untracked
    ON branch_inventory(branch_id) WHERE is_tracked = FALSE AND NOT is_deleted;
