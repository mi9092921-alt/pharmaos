-- 20260711000200_core_schema.sql
-- Phase 0 / M4 — Core schema: mandatory-column contract, multi-currency backbone,
-- branches, users, RBAC tables. Per CLAUDE.md v1.1.
--
-- Mandatory columns (CLAUDE.md "الأعمدة الإلزامية"):
--   id UUID PK, is_deleted (soft delete only), sync_version (bumped by trigger),
--   created_at/updated_at, created_by/updated_by REFERENCES users(id).
-- Catalog/reference tables are global (no branch_id). Operational tables (Phase 1+)
-- will carry branch_id NOT NULL.
--
-- PK exception (documented): currencies/countries use the natural keys given by the
-- explicit DDL in CLAUDE.md (CHAR(3)/CHAR(2)); all other mandatory columns applied
-- per the table-classification rules.

-- ============================================================================
-- 1) Row-touch trigger: maintains updated_at and a MONOTONIC sync_version.
--    Forbidden action #2 ("لا تغيّر sync_version يدوياً") is enforced here:
--    whatever the UPDATE sets, sync_version is forced to OLD.sync_version + 1.
-- ============================================================================
CREATE OR REPLACE FUNCTION touch_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at   := NOW();
    NEW.sync_version := OLD.sync_version + 1;
    RETURN NEW;
END;
$$;

-- ============================================================================
-- 2) Users — created first (mandatory-column FKs point here).
--    Auth backbone per CLAUDE.md: argon2id hash, token_version for session
--    revocation, lockout counters (5 attempts -> 15 minutes).
--    users.phone is field-encrypted (AES-256-GCM, service layer) -> BYTEA.
-- ============================================================================
CREATE TABLE users (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username              VARCHAR(50) NOT NULL,
    full_name             VARCHAR(255) NOT NULL,
    phone_encrypted       BYTEA,
    password_hash         TEXT NOT NULL,
    token_version         INTEGER NOT NULL DEFAULT 0,
    failed_login_attempts SMALLINT NOT NULL DEFAULT 0,
    locked_until          TIMESTAMPTZ,
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,

    is_deleted            BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version          BIGINT NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by            UUID REFERENCES users(id),
    updated_by            UUID REFERENCES users(id),

    CONSTRAINT uq_users_username UNIQUE (username)
);
CREATE TRIGGER trg_users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 3) Multi-currency backbone (ISO 4217 / ISO 3166-1). Egypt/EGP is the default
--    by SEED, not by structure — any world currency is supported by design.
-- ============================================================================
CREATE TABLE currencies (
    code            CHAR(3) PRIMARY KEY,          -- ISO 4217: EGP, SAR, AED, USD...
    name_ar         VARCHAR(50) NOT NULL,
    symbol          VARCHAR(8) NOT NULL,          -- ج.م / ر.س / د.إ
    decimal_places  SMALLINT NOT NULL DEFAULT 2,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);
CREATE TRIGGER trg_currencies_touch BEFORE UPDATE ON currencies
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE tax_profiles (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(80) NOT NULL,     -- e.g. "VAT مصر 14%" — a setting, not a constant
    vat_rate            DECIMAL(5,2) NOT NULL,
    medicine_vat_rate   DECIMAL(5,2),             -- medicine may be exempt/reduced per country
    einvoice_system     VARCHAR(20),              -- 'eta_ereceipt' | 'zatca' | NULL

    is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version        BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID REFERENCES users(id),
    updated_by          UUID REFERENCES users(id)
);
CREATE TRIGGER trg_tax_profiles_touch BEFORE UPDATE ON tax_profiles
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE countries (
    code                CHAR(2) PRIMARY KEY,      -- ISO 3166-1: EG, SA, AE...
    name_ar             VARCHAR(80) NOT NULL,
    default_currency    CHAR(3) NOT NULL REFERENCES currencies(code),
    tax_profile_id      UUID REFERENCES tax_profiles(id),
    calendar            VARCHAR(10) NOT NULL DEFAULT 'gregory',  -- gregory | islamic

    is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version        BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          UUID REFERENCES users(id),
    updated_by          UUID REFERENCES users(id)
);
CREATE TRIGGER trg_countries_touch BEFORE UPDATE ON countries
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 4) Branches — each branch inherits country/currency from its configuration
--    (CLAUDE.md multi-currency rules), configurable per branch.
-- ============================================================================
CREATE TABLE branches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    country_code    CHAR(2) NOT NULL REFERENCES countries(code),
    currency_code   CHAR(3) NOT NULL REFERENCES currencies(code),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);
CREATE TRIGGER trg_branches_touch BEFORE UPDATE ON branches
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 5) RBAC tables. The permission MATRIX is defined in code
--    (packages/shared/permissions.ts) and SEEDED into these tables on every
--    migration run — code always wins over manual DB edits (CLAUDE.md RBAC v1.1).
--    Custom roles (is_system = FALSE) are created in the DB on top of the
--    code-defined permissions.
-- ============================================================================
CREATE TABLE roles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code            VARCHAR(50) NOT NULL,         -- SystemRole codes for built-ins
    name_ar         VARCHAR(80) NOT NULL,
    is_system       BOOLEAN NOT NULL DEFAULT FALSE,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_roles_code UNIQUE (code)
);
CREATE TRIGGER trg_roles_touch BEFORE UPDATE ON roles
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE permissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code            VARCHAR(100) NOT NULL,        -- e.g. 'inventory.view' — UI translates

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_permissions_code UNIQUE (code)
);
CREATE TRIGGER trg_permissions_touch BEFORE UPDATE ON permissions
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE role_permissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id         UUID NOT NULL REFERENCES roles(id),
    permission_id   UUID NOT NULL REFERENCES permissions(id),

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_role_permissions UNIQUE (role_id, permission_id)
);
CREATE TRIGGER trg_role_permissions_touch BEFORE UPDATE ON role_permissions
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE INDEX idx_role_permissions_role ON role_permissions(role_id) WHERE NOT is_deleted;
CREATE INDEX idx_users_active ON users(username) WHERE NOT is_deleted AND is_active;

-- ============================================================================
-- 6) Reference seeds — Egypt defaults (idempotent; fixed UUIDs for seed rows).
--    Values are SETTINGS seeded as the current defaults, not structural constants.
-- ============================================================================
INSERT INTO currencies (code, name_ar, symbol, decimal_places)
VALUES ('EGP', 'جنيه مصري', 'ج.م', 2)
ON CONFLICT (code) DO NOTHING;

INSERT INTO tax_profiles (id, name, vat_rate, medicine_vat_rate, einvoice_system)
VALUES ('00000000-0000-4000-8000-000000000001', 'VAT مصر 14%', 14.00, NULL, 'eta_ereceipt')
ON CONFLICT (id) DO NOTHING;

INSERT INTO countries (code, name_ar, default_currency, tax_profile_id, calendar)
VALUES ('EG', 'مصر', 'EGP', '00000000-0000-4000-8000-000000000001', 'gregory')
ON CONFLICT (code) DO NOTHING;
