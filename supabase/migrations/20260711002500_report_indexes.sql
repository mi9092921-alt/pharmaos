-- 20260711002500_report_indexes.sql
-- Phase 3 / P3-M1 — reporting foundation (sales reports: daily/monthly/annual).
--
-- Phase 3 reports are on-demand SQL aggregations (decision D2), each scoped to a
-- branch and a LOCAL-DAY date RANGE over invoices.created_at — the Z-report
-- convention (cashier_service.day_report) generalized from a single day to a
-- range. The existing idx_invoices_date is (created_at DESC, branch_id): it
-- leads with the timestamp, so a single-branch range scan would range the whole
-- table's dates and only then filter branch_id. Sales reports are ALWAYS
-- branch-first, then time-ranged, so the covering index for that access pattern
-- leads with branch_id:
--   (branch_id, created_at) — an equality seek on the branch, then a range on
--   time. Partial on NOT is_deleted mirrors the soft-delete convention and keeps
--   the index tight (nothing is hard-deleted; completed invoices accumulate for
--   the life of the branch).
--
-- This single index serves every Phase-3 invoice report (M1 sales trend/summary
-- + top items via the invoice_items join, M4 P&L), keeping the daily report
-- within the < 3s budget (CLAUDE.md perf targets). Refund aggregation reuses the
-- existing idx_returns_branch (branch_id, created_at DESC).

CREATE INDEX idx_invoices_branch_created ON invoices(branch_id, created_at)
    WHERE NOT is_deleted;
