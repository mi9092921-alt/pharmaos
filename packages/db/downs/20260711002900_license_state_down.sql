-- 20260711002900_license_state_down.sql
-- Paired down for the licensing core (P4-M1). Pure addition — drop in reverse.

DROP TABLE IF EXISTS license_clock_events;
DROP TABLE IF EXISTS license_state;
DROP FUNCTION IF EXISTS validate_license_clock_chain();
DROP FUNCTION IF EXISTS forbid_license_clock_truncate();
DROP FUNCTION IF EXISTS forbid_license_clock_mutation();
