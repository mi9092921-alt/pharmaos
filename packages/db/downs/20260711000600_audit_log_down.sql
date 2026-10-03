-- Down migration for 20260711000600_audit_log.sql
--
-- The `app_user` ROLE is intentionally NOT dropped: roles are cluster-global
-- infrastructure (not schema objects) and may be referenced outside this
-- migration. The up migration creates it idempotently, so re-up is safe.

DROP TRIGGER IF EXISTS trg_audit_immutable ON audit_logs;
DROP FUNCTION IF EXISTS forbid_audit_mutation();
DROP TABLE IF EXISTS audit_logs;
