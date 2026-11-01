-- Down for 20260711003200_catalog_search_trgm_raw:
-- the up added a trigram GIN index serving raw `trade_name ILIKE '%q%'`.
-- Index-only revert — no data touched; the IF EXISTS keeps it re-runnable.
DROP INDEX IF EXISTS idx_medications_trgm_raw;
