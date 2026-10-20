-- 20261020000100_fuzzystrmatch_arabic_search.sql
--
-- pg_trgm remains the indexed fast path for partial search.  On the bundled
-- PostgreSQL C.UTF-8 locale, pg_trgm does not produce useful trigrams for
-- Arabic code points, so the catalog service also uses a bounded edit-distance
-- fallback for one-character Arabic typos.

CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
