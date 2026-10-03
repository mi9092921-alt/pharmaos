-- ETA e-receipt outbox queue (P2-M10 — الإيصال الإلكتروني).
--
-- CLAUDE.md §Egypt-compliance + pharmaos.md: the sale NEVER blocks on the
-- internet. On a completed sale (in a branch whose tax profile uses the ETA
-- e-receipt system) an ereceipt row is enqueued 'pending' INSIDE the sale
-- transaction (outbox pattern). A background worker (drained on demand here /
-- CLI / Celery in production) builds the JSON, signs it with the X.509 seal,
-- authenticates via OAuth2 client-credentials, submits it, and records the
-- returned UUID + QR. All of that runs against a PORT/ADAPTER whose default is a
-- LOCAL SIMULATOR — no real ETA acceptance is ever claimed without real
-- credentials (acceptance is marked "pending credentials"; see eta_adapter.py).
--
-- One e-receipt per invoice (UNIQUE invoice_id). Status is the standard outbox
-- lifecycle; a rejected/failed row can be retried (submission_attempts).

CREATE TABLE ereceipt_queue (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id            UUID NOT NULL REFERENCES branches(id),
    invoice_id           UUID NOT NULL REFERENCES invoices(id),
    status               VARCHAR(20) NOT NULL DEFAULT 'pending',
    payload              JSONB,
    signed_payload       TEXT,
    eta_uuid             VARCHAR(64),
    qr_data              TEXT,
    submission_attempts  INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    submitted_at         TIMESTAMPTZ,
    accepted_at          TIMESTAMPTZ,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_ereceipt_invoice UNIQUE (invoice_id),
    CONSTRAINT chk_ereceipt_status CHECK (status IN (
        'pending', 'building', 'signed', 'submitting',
        'submitted', 'accepted', 'rejected', 'failed'
    ))
);
CREATE TRIGGER trg_ereceipt_queue_touch BEFORE UPDATE ON ereceipt_queue
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- The worker drains rows that still need work, oldest first, per branch.
CREATE INDEX idx_ereceipt_queue_pending ON ereceipt_queue(branch_id, created_at)
    WHERE status IN ('pending', 'building', 'signed', 'submitting', 'submitted', 'failed');
