-- 20260711000300_catalog_schema.sql
-- Phase 0 / M5 — Catalog schema + Arabic full-text search infrastructure.
--
-- Catalog tables are GLOBAL (no branch_id, no stock quantities — CLAUDE.md v1.1):
-- categories, units, medications, medication_packaging, medication_barcodes.
-- Stock lives in branch_inventory/medication_batches (operational, Phase 1).
--
-- Phase 0 delivers the SCHEMA only; catalog CRUD, seeding (CC0 dataset) and the
-- search endpoints are Phase 1 features.

-- ============================================================================
-- 1) Arabic search infrastructure (CLAUDE.md "البحث العربي" — mandatory design).
--    PostgreSQL has NO built-in 'arabic' text search configuration;
--    to_tsvector('arabic', ...) fails immediately. A custom config based on
--    'simple' (no stemming — Arabic stemming is unsupported anyway) plus an
--    IMMUTABLE normalization function is the approved solution.
-- ============================================================================
CREATE TEXT SEARCH CONFIGURATION arabic_simple (COPY = simple);

-- Normalization (IMMUTABLE + PARALLEL SAFE — required for use in generated
-- columns and indexes):
--   * strips tashkeel (U+064B..U+0652) and superscript alef (U+0670)
--   * strips tatweel (ـ)
--   * unifies alef forms (أ إ آ ٱ -> ا), alef maqsura (ى -> ي), taa marbuta (ة -> ه)
-- The SAME rules must be applied to user queries (service layer) before matching.
CREATE OR REPLACE FUNCTION normalize_arabic(input TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
  SELECT translate(
    regexp_replace(
      regexp_replace(coalesce(input, ''), '[ً-ْٰ]', '', 'g'),  -- tashkeel + U+0670
      'ـ', '', 'g'),                                            -- tatweel
    'أإآٱىة', 'اااايه'                                            -- unify alef/yaa/taa
  );
$$;

-- ============================================================================
-- 2) Reference lookups: categories & units.
--    Bilingual naming follows the established schema pattern (ar primary,
--    en optional) — the system is ar/en bilingual per CLAUDE.md i18n.
-- ============================================================================
CREATE TABLE categories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name_ar         VARCHAR(120) NOT NULL,
    name_en         VARCHAR(120),

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);
CREATE TRIGGER trg_categories_touch BEFORE UPDATE ON categories
    FOR EACH ROW EXECUTE FUNCTION touch_row();

CREATE TABLE units (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name_ar         VARCHAR(50) NOT NULL,
    name_en         VARCHAR(50),

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id)
);
CREATE TRIGGER trg_units_touch BEFORE UPDATE ON units
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 3) Medications — global catalog (no branch_id, NO stock quantity — forbidden
--    action #15: quantities live on batches only).
--    search_vector is defined directly as the generated column specified by the
--    Arabic-search section (same expression as the doc's ALTER TABLE form).
-- ============================================================================
CREATE TABLE medications (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_name            VARCHAR(255) NOT NULL,
    trade_name_ar         VARCHAR(255),            -- Arabic name (search & display)
    scientific_name       VARCHAR(255),            -- active ingredient
    manufacturer          VARCHAR(255),
    category_id           UUID REFERENCES categories(id),
    drug_class            VARCHAR(100),
    route                 VARCHAR(50),              -- oral/topical/injection...

    -- Pharmacy specific
    requires_prescription BOOLEAN NOT NULL DEFAULT FALSE,
    controlled_substance  BOOLEAN NOT NULL DEFAULT FALSE,
    storage_conditions    VARCHAR(100),

    -- Regulatory (Egypt)
    eda_registration_no   VARCHAR(50),              -- EDA registration number
    gtin                  VARCHAR(14),              -- GS1 GTIN (drug track & trace)

    -- Arabic FTS (generated — CLAUDE.md Arabic search section)
    search_vector         TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('arabic_simple',
            normalize_arabic(coalesce(trade_name, '') || ' ' ||
                             coalesce(trade_name_ar, '') || ' ' ||
                             coalesce(scientific_name, ''))
        )
    ) STORED,

    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    is_deleted            BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version          BIGINT NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by            UUID REFERENCES users(id),
    updated_by            UUID REFERENCES users(id)
);
CREATE TRIGGER trg_medications_touch BEFORE UPDATE ON medications
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 4) Packaging hierarchy: box -> strip -> tablet (essential for Egypt — the
--    pharmacy sells by strip and tablet, not only by box). Level pricing &
--    default POS sale level per CLAUDE.md v1.1. All stock quantities are stored
--    in the SMALLEST unit; conversion goes through qty_in_parent.
-- ============================================================================
CREATE TABLE medication_packaging (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    level           SMALLINT NOT NULL,            -- 1=box, 2=strip, 3=tablet/unit
    unit_id         UUID NOT NULL REFERENCES units(id),
    name_ar         VARCHAR(50) NOT NULL,         -- "علبة" / "شريط" / "قرص"
    qty_in_parent   DECIMAL(10,3),                -- units of this level inside the parent
                                                  -- (box=NULL; strip: strips/box; tablet: tablets/strip)
    is_sellable     BOOLEAN NOT NULL DEFAULT TRUE,
    selling_price   DECIMAL(12,2) NOT NULL,       -- selling price for this level (DECIMAL — never FLOAT)
    is_default_sale BOOLEAN NOT NULL DEFAULT FALSE,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_packaging_med_level UNIQUE (medication_id, level)
);
CREATE TRIGGER trg_medication_packaging_touch BEFORE UPDATE ON medication_packaging
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 5) Barcodes — multiple per medication (one per packaging level + alternates).
--    UNIQUE(barcode) is safe in v1.1: the catalog is one global set, no
--    duplication across branches.
-- ============================================================================
CREATE TABLE medication_barcodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    medication_id   UUID NOT NULL REFERENCES medications(id),
    packaging_id    UUID REFERENCES medication_packaging(id),
    barcode         VARCHAR(64) NOT NULL,
    barcode_type    VARCHAR(20) NOT NULL DEFAULT 'EAN13',  -- EAN13 | GS1_DATAMATRIX | CODE128
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,

    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    sync_version    BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    updated_by      UUID REFERENCES users(id),

    CONSTRAINT uq_barcodes_barcode UNIQUE (barcode)
);
CREATE TRIGGER trg_medication_barcodes_touch BEFORE UPDATE ON medication_barcodes
    FOR EACH ROW EXECUTE FUNCTION touch_row();

-- ============================================================================
-- 6) Indexes (CLAUDE.md performance rules — plain CREATE INDEX in creation
--    migrations; CONCURRENTLY cannot run inside a transaction).
-- ============================================================================
-- Fastest path: exact barcode match (scan -> display < 50ms).
CREATE INDEX idx_barcodes_barcode ON medication_barcodes(barcode);
-- Arabic FTS + trigram fallback (partial matches / typos).
CREATE INDEX idx_medications_fts ON medications USING GIN(search_vector);
CREATE INDEX idx_medications_trgm ON medications
    USING GIN (normalize_arabic(trade_name_ar) gin_trgm_ops);
-- Constant catalog access paths (cross-cutting rule: indexes on all tables).
CREATE INDEX idx_packaging_medication ON medication_packaging(medication_id) WHERE NOT is_deleted;
CREATE INDEX idx_barcodes_medication ON medication_barcodes(medication_id) WHERE NOT is_deleted;
