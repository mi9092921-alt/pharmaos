-- Release-prep phase 3 (scale test finding): the catalog search fallback runs
-- `trade_name ILIKE '%q%'` on the RAW Latin name, which had no index — the OR
-- with the normalized-name expressions forced a seq scan with per-row regex
-- over the whole catalog (264ms measured at 10k SKUs). gin_trgm_ops serves
-- ILIKE '%q%' through an index. Non-destructive, IF NOT EXISTS for idempotence.
CREATE INDEX IF NOT EXISTS idx_medications_trgm_raw
    ON medications USING gin (trade_name gin_trgm_ops);
