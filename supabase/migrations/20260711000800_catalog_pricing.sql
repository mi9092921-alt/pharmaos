-- 20260711000800_catalog_pricing.sql
-- Phase 1 / M5 — price history + price provenance + base units.
--
-- Egypt compliance (CLAUDE.md): medicine prices are GOVERNMENT-SET; every price
-- carries provenance (price_source) and an update timestamp, and every change
-- is recorded in a history log ("سجل تاريخ أسعار لكل تغيير").

-- 1) Price provenance on the price-bearing table (medication_packaging).
ALTER TABLE medication_packaging
    ADD COLUMN price_source VARCHAR(30) NOT NULL DEFAULT 'manual',
    ADD COLUMN price_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- 2) Price history — catalog-scoped append log (mandatory catalog columns).
CREATE TABLE medication_price_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    packaging_id    UUID NOT NULL REFERENCES medication_packaging(id),
    old_price       DECIMAL(12,2),                -- NULL = first price
    new_price       DECIMAL(12,2) NOT NULL,
    price_source    VARCHAR(30) NOT NULL DEFAULT 'manual',  -- manual | seed | import | provider

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);
CREATE TRIGGER trg_price_history_touch BEFORE UPDATE ON medication_price_history
    FOR EACH ROW EXECUTE FUNCTION touch_row();
CREATE INDEX idx_price_history_med ON medication_price_history(medication_id, created_at DESC);

-- 3) Units: unique Arabic name + seed the three levels named by the spec
--    (علبة/شريط/قرص). Idempotent.
ALTER TABLE units ADD CONSTRAINT uq_units_name_ar UNIQUE (name_ar);
INSERT INTO units (name_ar, name_en) VALUES
    ('علبة', 'Box'), ('شريط', 'Strip'), ('قرص', 'Tablet')
ON CONFLICT (name_ar) DO NOTHING;
