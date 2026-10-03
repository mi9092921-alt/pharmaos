-- Down migration for 20260711000100_extensions.sql
-- Rollback: remove the extensions (safe only when no dependent objects remain;
-- the up/down verifier runs downs in strict reverse order, guaranteeing that).

DROP EXTENSION IF EXISTS pg_trgm;
DROP EXTENSION IF EXISTS pgcrypto;
