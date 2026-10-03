-- 20260711002800_notifications.sql
-- Phase 3 / P3-M7 — notifications (DELIVERY) — ratified decision D4 keeps this
-- separate from alerts (STATE, migration 2700): an alert is "what is wrong",
-- a notification is "how a human is told about it".
--
-- Channels (ratified D5): in_app + desktop are delivered in Phase 3 (sent_at
-- set on creation — the client consumes them); email rows are QUEUED behind a
-- provider gateway (sent_at stays NULL until a configured provider actually
-- sends — the compliance-adapter pattern; acceptance is "pending provider").
-- SMS is deferred to Phase 4 (never claimed here).
--
-- title_key/body_key + params: the API never ships localized strings — the
-- client renders t(title_key)/t(body_key) with {token} interpolation, the same
-- contract alerts use.
--
-- user_id is NULLABLE: a row targets ONE user or broadcasts to the branch's
-- notification audience (user_id IS NULL). The schema has no user↔branch
-- membership table yet, so branch-wide fan-out to individuals is not
-- enumerable — broadcast rows with a shared read_at are the honest MVP
-- (per-user fan-out lands with membership).
--
-- related_alert_id links a notification back to the alert that spawned it
-- (the alerts engine is the primary producer in Phase 3).

CREATE TABLE notifications (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id        UUID NOT NULL REFERENCES branches(id),
    user_id          UUID REFERENCES users(id),   -- NULL = branch broadcast
    channel          VARCHAR(10) NOT NULL DEFAULT 'in_app',
    priority         VARCHAR(10) NOT NULL DEFAULT 'medium',
    title_key        VARCHAR(60) NOT NULL,
    body_key         VARCHAR(60) NOT NULL,
    params           JSONB NOT NULL DEFAULT '{}'::jsonb,
    read_at          TIMESTAMPTZ,
    sent_at          TIMESTAMPTZ,                 -- in_app/desktop: NOW() at create; email: when the provider sends
    related_alert_id UUID REFERENCES alerts(id),

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_notification_channel CHECK (channel IN ('in_app', 'desktop', 'email')),
    CONSTRAINT chk_notification_priority CHECK (priority IN ('low', 'medium', 'high', 'critical'))
);
CREATE TRIGGER trg_notifications_touch BEFORE UPDATE ON notifications
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- Bell/list hot path: newest-first for a branch.
CREATE INDEX idx_notifications_branch_created
    ON notifications(branch_id, created_at DESC) WHERE NOT is_deleted;
-- Unread counter: live unread rows per branch/user (broadcasts have NULL user).
CREATE INDEX idx_notifications_unread
    ON notifications(branch_id, user_id) WHERE read_at IS NULL AND NOT is_deleted;
