-- Down migration for 20260711001400_batch_status_index.sql
DROP INDEX IF EXISTS idx_batches_branch_status;
