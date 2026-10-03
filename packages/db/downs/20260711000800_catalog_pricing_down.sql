-- Down migration for 20260711000800_catalog_pricing.sql
DROP TABLE IF EXISTS medication_price_history;
ALTER TABLE medication_packaging
    DROP COLUMN IF EXISTS price_source,
    DROP COLUMN IF EXISTS price_updated_at;
ALTER TABLE units DROP CONSTRAINT IF EXISTS uq_units_name_ar;
-- Seeded unit rows remain (soft-delete-only policy; harmless reference data).
