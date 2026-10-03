-- 20260711000100_extensions.sql
-- Phase 0 / M3 — required PostgreSQL extensions.
--
-- pgcrypto : required by pharmaos.md Phase 0 ("تفعيل امتداد pgcrypto");
--            also provides gen_random_uuid() on older lines (built-in on PG17,
--            the extension keeps parity and enables digest()/crypt helpers).
-- pg_trgm  : trigram indexes for Arabic partial/typo search (CLAUDE.md Arabic FTS).

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
