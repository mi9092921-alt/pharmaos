-- 20260711002900_license_state.sql
-- Phase 4 / P4-M1 — licensing core (docs/phase4-execution-plan-licensing.md §1/§5).
--
-- license_state: the DEVICE-GLOBAL licensing singleton. A second row is
-- impossible at the DB level — `singleton_key` is CHECK(TRUE) + UNIQUE — never
-- just a "SELECT … LIMIT 1" convention. Carries the full mandatory-column
-- contract (it is an updatable state table; created_by/updated_by stay NULL
-- because activation happens pre-auth).
--
-- license_clock_events: the append-only clock-event ledger. Like audit_logs
-- (migration …000600) it is DELIBERATELY exempt from the mandatory-column
-- contract: no is_deleted/updated_*/sync_version — the immutability and
-- chain-integrity triggers forbid the UPDATEs those imply. seq is the PRIMARY
-- KEY (CHECK seq > 0); prev_hash is NULL exactly once, at genesis.
--
-- Enforcement split (P4 §1 — LOCK-1b):
--   * DB triggers enforce STRUCTURE: append-only (UPDATE/DELETE/TRUNCATE
--     forbidden for every role incl. the owner), seq monotonic, and the
--     prev_hash/seq linkage under pg_advisory_xact_lock(740029001) — the same
--     lock the application takes BEFORE reading the head and computing the
--     HMAC, so concurrent appends serialize instead of forking.
--   * Hash AUTHENTICITY is application-enforced: verify_chain re-computes
--     every entry_hash with the keystore key (a forged well-linked row is
--     detected; crafting a chain that verifies requires the key). The trigger
--     cannot check HMACs — the key never enters the database (P4 §1).
--   * REVOKE INSERT/UPDATE/DELETE/TRUNCATE FROM app_user is defense-in-depth
--     on top of the triggers (the default connection role is `pharmaos`).

-- ----------------------------------------------------------------------------
-- license_state (singleton)
-- ----------------------------------------------------------------------------
CREATE TABLE license_state (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    singleton_key              BOOLEAN NOT NULL DEFAULT TRUE CONSTRAINT chk_license_state_singleton_key CHECK (singleton_key),
    license_id                 VARCHAR(32),
    customer_name              VARCHAR(200),
    hwid                       VARCHAR(32),
    kid                        VARCHAR(16),
    payload                    JSONB NOT NULL DEFAULT '{}'::jsonb,
    signature                  TEXT NOT NULL DEFAULT '',
    status                     VARCHAR(20) NOT NULL DEFAULT 'unlicensed',
    activated_at               TIMESTAMPTZ,
    last_seen_utc              TIMESTAMPTZ,
    high_water_utc             TIMESTAMPTZ,
    anomaly_count              INTEGER NOT NULL DEFAULT 0,
    tamper_flag                BOOLEAN NOT NULL DEFAULT FALSE,
    verified_from_seq          BIGINT,
    last_activation_issued_at  TIMESTAMPTZ,
    last_activation_license_id VARCHAR(32),
    external_sync_state        JSONB NOT NULL DEFAULT '{}'::jsonb,

    is_deleted   BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version BIGINT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by   UUID REFERENCES users(id),
    updated_by   UUID REFERENCES users(id),

    CONSTRAINT uq_license_state_singleton UNIQUE (singleton_key),
    CONSTRAINT chk_license_state_status CHECK (status IN (
        'unlicensed', 'active', 'grace', 'read_only',
        'clock_error', 'key_lost', 'error', 'tamper'))
);

CREATE TRIGGER trg_license_state_touch BEFORE UPDATE ON license_state
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ----------------------------------------------------------------------------
-- license_clock_events (append-only ledger — exempt from mandatory columns,
-- same rationale as audit_logs in migration …000600)
-- ----------------------------------------------------------------------------
CREATE TABLE license_clock_events (
    seq            BIGINT PRIMARY KEY CHECK (seq > 0),
    event_type     VARCHAR(30) NOT NULL,
    origin         VARCHAR(20) NOT NULL,
    observed_at    TIMESTAMPTZ NOT NULL,
    high_water_utc TIMESTAMPTZ NOT NULL,
    ref            VARCHAR(64) NOT NULL DEFAULT '',
    anomaly_count  INTEGER NOT NULL DEFAULT 0,
    prev_hash      VARCHAR(64),                 -- NULL exactly once: genesis
    entry_hash     VARCHAR(64) NOT NULL,        -- 64 lowercase hex chars

    CONSTRAINT chk_license_clock_event_type CHECK (event_type IN (
        'boot_seen', 'rollback_detected', 'source_regression',
        'activation', 'state_changed')),
    CONSTRAINT chk_license_clock_origin CHECK (origin IN (
        'boot', 'activation', 'periodic', 'reconciliation')),
    CONSTRAINT chk_license_clock_hash_hex CHECK (
        entry_hash ~ '^[0-9a-f]{64}$'
        AND (prev_hash IS NULL OR prev_hash ~ '^[0-9a-f]{64}$'))
);

-- ----------------------------------------------------------------------------
-- Structure enforcement: append-only + chain integrity under the advisory lock
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION forbid_license_clock_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'license_clock_events is append-only';
END;
$$;

CREATE TRIGGER trg_license_clock_immutable
    BEFORE UPDATE OR DELETE ON license_clock_events
    FOR EACH ROW EXECUTE FUNCTION forbid_license_clock_mutation();

CREATE OR REPLACE FUNCTION forbid_license_clock_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'license_clock_events cannot be truncated';
END;
$$;

CREATE TRIGGER trg_license_clock_no_truncate
    BEFORE TRUNCATE ON license_clock_events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_license_clock_truncate();

-- Chain linkage: the application holds the SAME advisory lock before reading
-- the head and computing the HMAC (so concurrent appends serialize instead of
-- forking); the trigger re-acquires it (no-op in the same transaction) and
-- validates — it NEVER rewrites the proposed seq/prev_hash.
CREATE OR REPLACE FUNCTION validate_license_clock_chain()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    last_seq        BIGINT;
    last_entry_hash VARCHAR(64);
BEGIN
    PERFORM pg_advisory_xact_lock(740029001);

    IF NEW.seq IS NULL OR NEW.seq <= 0 THEN
        RAISE EXCEPTION 'license_clock_events.seq must be a positive integer';
    END IF;

    SELECT seq, entry_hash INTO last_seq, last_entry_hash
    FROM license_clock_events ORDER BY seq DESC LIMIT 1;

    IF last_seq IS NULL THEN
        IF NEW.seq <> 1 THEN
            RAISE EXCEPTION 'license_clock_events: genesis row must have seq = 1 (got %)', NEW.seq;
        END IF;
        IF NEW.prev_hash IS NOT NULL THEN
            RAISE EXCEPTION 'license_clock_events: genesis row must have NULL prev_hash';
        END IF;
    ELSE
        IF NEW.seq <> last_seq + 1 THEN
            RAISE EXCEPTION 'license_clock_events: seq must be % (got %)', last_seq + 1, NEW.seq;
        END IF;
        IF NEW.prev_hash IS NULL OR NEW.prev_hash <> last_entry_hash THEN
            RAISE EXCEPTION 'license_clock_events: prev_hash must reference the last entry (% )', last_entry_hash;
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_license_clock_chain
    BEFORE INSERT ON license_clock_events
    FOR EACH ROW EXECUTE FUNCTION validate_license_clock_chain();

-- Defense-in-depth privilege revokes (audit_logs pattern, migration …000600;
-- app_user is created idempotently there — recreate guard for fresh scratch
-- DBs that apply migrations in a different order through this runner).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user NOLOGIN;
    END IF;
END;
$$;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON license_clock_events FROM app_user;
REVOKE UPDATE, DELETE, TRUNCATE ON license_state FROM app_user;
