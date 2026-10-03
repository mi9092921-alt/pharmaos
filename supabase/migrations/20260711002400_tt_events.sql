-- EDA Track & Trace event outbox (P2-M11 — منظومة تتبّع الدواء).
--
-- Decrees 161/475/804 of 2025: every 2D-serialized pack is tracked from
-- production to dispensing. PharmaOS reports receive/dispense/destroy events
-- (and imports pre-launch manual records) through an outbox queue drained to
-- the EDA adapter (local simulator by default; see eda_tt_adapter.py). Events
-- are enqueued 'pending' inside the receive/sale transaction — capture and
-- dispensing never block on the national system.
--
-- One event per (pack, event_type) occurrence. pack_serial_id is nullable so a
-- pre-launch 'import' record can be reported before its pack row exists.

CREATE TABLE tt_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id        UUID NOT NULL REFERENCES branches(id),
    event_type       VARCHAR(20) NOT NULL,
    pack_serial_id   UUID REFERENCES pack_serials(id),
    gtin             VARCHAR(14) NOT NULL,
    serial_number    VARCHAR(64) NOT NULL,
    batch_number     VARCHAR(50),
    expiry_date      DATE,
    invoice_id       UUID REFERENCES invoices(id),
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
    report_attempts  INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    reported_at      TIMESTAMPTZ,
    payload          JSONB,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT chk_tt_event_type CHECK (event_type IN ('receive', 'dispense', 'destroy', 'import')),
    CONSTRAINT chk_tt_status CHECK (status IN ('pending', 'reported', 'failed'))
);
CREATE TRIGGER trg_tt_events_touch BEFORE UPDATE ON tt_events
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- The worker drains events that still need reporting, oldest first, per branch.
CREATE INDEX idx_tt_events_pending ON tt_events(branch_id, created_at)
    WHERE status IN ('pending', 'failed');
