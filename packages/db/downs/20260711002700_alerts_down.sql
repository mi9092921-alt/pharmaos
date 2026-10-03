-- 20260711002700_alerts_down.sql
-- Paired down for the alerts table (P3-M6). Pure addition — drop in reverse.

DROP INDEX IF EXISTS idx_alerts_branch_status;
DROP INDEX IF EXISTS uq_alerts_dedup_active;
DROP TABLE IF EXISTS alerts;
