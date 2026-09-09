-- 20260711002600_inventory_report_indexes.sql
-- Phase 3 / P3-M2 — inventory reports (stock level / movement analysis /
-- valuation / slow-fast movers).
--
-- Stock-level and valuation reports reuse EXISTING indexes and need nothing new:
--   * idx_inventory_branch      (branch_id, medication_id) on branch_inventory
--     serves the stock-level report's branch scan.
--   * idx_batches_branch_med    (branch_id, medication_id) WHERE NOT is_deleted
--     AND status = 'active' on medication_batches already covers exactly the
--     "active batches for this branch" scan the valuation report groups by
--     medication_id (same predicate batch_status_report's sellable_value uses).
--
-- Movement analysis (by movement_type over a date range) and slow/fast movers
-- (top-N medications by 'sale_out' volume over a range) both filter
-- stock_movements by branch + movement_type + a created_at range — a shape none
-- of the existing indexes serve (idx_movements_batch is batch_id-only). This
-- mirrors idx_invoices_branch_created (P3-M1) but adds movement_type as the
-- second column since the reports' dominant access pattern is "one type at a
-- time" (sale_out for movers; each type's own row in the by-type breakdown),
-- exactly as pharmaos-phase-3-execution-plan-analytics.md names it.

CREATE INDEX idx_movements_branch_type_created
    ON stock_movements(branch_id, movement_type, created_at)
    WHERE NOT is_deleted;
