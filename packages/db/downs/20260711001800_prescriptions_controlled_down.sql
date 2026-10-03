-- Down migration for 20260711001800_prescriptions_controlled.sql
-- Reverse order: controlled_substance_log (+ its trigger/function) first, then
-- prescription_items, then prescriptions.
DROP TRIGGER IF EXISTS trg_controlled_substance_immutable ON controlled_substance_log;
DROP FUNCTION IF EXISTS forbid_controlled_substance_mutation();
DROP TABLE IF EXISTS controlled_substance_log;
DROP TABLE IF EXISTS prescription_items;
DROP TABLE IF EXISTS prescriptions;
