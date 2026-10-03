-- 20260711001400_batch_status_index.sql
-- Phase 2 / P2-M4 — batch tracking deepening (expiry alerts + batch reports).
--
-- The near-expiry ALERT scan (active batches by expiry horizon) is already
-- served by idx_batches_expiry (branch_id, expiry_date) WHERE status = 'active'.
--
-- This adds the companion index for the batch STATUS reports. The "locked-up
-- stock" report filters medication_batches for a branch by a NON-active status
-- (quarantined | expired | recalled) — a selective slice that the active-only
-- partial indexes (idx_batches_branch_med / idx_batches_expiry) do NOT serve.
-- (branch_id, status) keeps those report scans index-backed as depleted/expired
-- batches accumulate over a branch's lifetime (every receipt is a batch; nothing
-- is hard-deleted). Partial on NOT is_deleted mirrors the soft-delete convention.

CREATE INDEX idx_batches_branch_status ON medication_batches(branch_id, status)
    WHERE NOT is_deleted;
