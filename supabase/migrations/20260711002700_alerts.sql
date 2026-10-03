-- 20260711002700_alerts.sql
-- Phase 3 / P3-M6 — smart alerts engine (CLAUDE.md ALERT_RULES, ratified D4).
--
-- alerts = the alert STATE table (what is wrong right now); notifications
-- (migration 2800, P3-M7) are the DELIVERY table — the two are deliberately
-- separate per ratified decision D4. An alert is generated IDEMPOTENTLY by
-- alerts_service rule evaluators: each (rule_key, entity) pair maps to a
-- deterministic dedup_key that is UNIQUE among ACTIVE alerts, so repeated
-- evaluation never duplicates rows (plan convention: alerts must never slow or
-- spam the primary flows — generation is at boot / CLI / POST /alerts/evaluate,
-- ratified D6, no scheduler in Phase 3).
--
-- Lifecycle: active → acknowledged (a human saw it — alerts.manage) → resolved
-- (the condition cleared or was handled). Acknowledging is NOT an audit event
-- (ratified D7 — the closed audit log gains no actions for alerts).
--
-- branch_id is NOT NULL (operational table). Device-global rules
-- (backup_overdue, sync_failed) anchor per-branch: every branch of the device
-- carries its own alert row so each branch's staff sees it.
--
-- severity follows CLAUDE.md's ALERT_RULES vocabulary exactly:
--   warning | danger | critical

CREATE TABLE alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches(id),
    rule_key        VARCHAR(40) NOT NULL,               -- ALERT_RULES key
    severity        VARCHAR(10) NOT NULL,
    entity_type     VARCHAR(40),                        -- medication | batch | invoice | cash_session | branch | system
    entity_id       UUID,                               -- NULL = branch-scoped condition
    message_key     VARCHAR(60) NOT NULL,               -- i18n key; the client renders localized
    params          JSONB NOT NULL DEFAULT '{}'::jsonb, -- interpolation params (names, quantities, ...)
    status          VARCHAR(15) NOT NULL DEFAULT 'active',
    dedup_key       VARCHAR(160) NOT NULL,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_by UUID REFERENCES users(id),
    acknowledged_at TIMESTAMPTZ,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_alert_severity CHECK (severity IN ('warning', 'danger', 'critical')),
    CONSTRAINT chk_alert_status CHECK (status IN ('active', 'acknowledged', 'resolved'))
);
CREATE TRIGGER trg_alerts_touch BEFORE UPDATE ON alerts
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- The idempotency spine: a rule+entity pair can have at most ONE live alert.
-- Partial unique (WHERE status <> 'resolved') — a resolved alert may recur
-- later (condition came back) and gets a FRESH row with new first_seen.
CREATE UNIQUE INDEX uq_alerts_dedup_active
    ON alerts(branch_id, dedup_key) WHERE status <> 'resolved';

-- List screen: active/acknowledged alerts per branch, newest condition first.
CREATE INDEX idx_alerts_branch_status
    ON alerts(branch_id, status, last_seen DESC) WHERE NOT is_deleted;
