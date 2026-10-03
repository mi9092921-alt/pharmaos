-- 20260711000600_audit_log.sql
-- Phase 1 / M1 — Audit Log (CLAUDE.md: "Audit Log فعّال من أول عملية" +
-- append-only بقيد قاعدة بيانات + سياسة احتفاظ).
--
-- audit_logs is DELIBERATELY exempt from the mandatory-column contract:
-- an append-only, immutable ledger cannot carry updated_at/updated_by/
-- is_deleted (no updates) nor a trigger-bumped sync_version (the touch_row
-- trigger issues an UPDATE, which the immutability trigger forbids). Append-only
-- logs replicate to the cloud by INSERT, not by Last-Write-Wins, so sync_version
-- is intentionally absent (cloud sync is Phase 4).
--
-- branch_id is NULLABLE here (unlike ordinary operational tables): several
-- audited operations are system-level and have no branch context
-- (user.created, settings.changed, backup.created, sync.failed).
--
-- Actor identity is stored BOTH as a FK (actor_user_id) and as a username
-- SNAPSHOT (actor_username) so the record stays meaningful even if the user is
-- later renamed or deactivated — the audit trail must be self-contained.

CREATE TABLE audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action          VARCHAR(50) NOT NULL,          -- code from the AUDITED_OPERATIONS registry
    actor_user_id   UUID REFERENCES users(id),     -- who (nullable: system / unauthenticated)
    actor_username  VARCHAR(50),                   -- snapshot of the actor's username
    branch_id       UUID REFERENCES branches(id),  -- nullable: system-level events
    entity_type     VARCHAR(50),                   -- e.g. 'invoice', 'user', 'backup'
    entity_id       UUID,                          -- affected record id
    ip_address      INET,                          -- request origin (nullable)
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- structured context — NO sensitive data
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Read/reporting access paths.
CREATE INDEX idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX idx_audit_action ON audit_logs(action, created_at DESC);
CREATE INDEX idx_audit_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX idx_audit_branch ON audit_logs(branch_id, created_at DESC);
CREATE INDEX idx_audit_actor ON audit_logs(actor_user_id, created_at DESC);

-- ============================================================================
-- Append-only enforcement at the DATABASE level (CLAUDE.md — not just an app
-- convention). The trigger fires for EVERY role (owner/superuser included),
-- so it is the primary, role-independent protection.
-- ============================================================================
CREATE OR REPLACE FUNCTION forbid_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only';
END;
$$;

CREATE TRIGGER trg_audit_immutable
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();

-- Defense-in-depth privilege revoke (CLAUDE.md).
-- The least-privilege application role `app_user` is created idempotently here
-- so the documented REVOKE is executable. Making the application actually
-- CONNECT as app_user (instead of the DB owner) is the security-hardening
-- milestone (P1-M11). NOTE for Supabase cloud: role management there is
-- provider-controlled — reconcile this role when the cloud project is linked.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user NOLOGIN;
    END IF;
END;
$$;

REVOKE UPDATE, DELETE ON audit_logs FROM app_user;
