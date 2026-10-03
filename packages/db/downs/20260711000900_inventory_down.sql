-- Down migration for 20260711000900_inventory.sql
DROP TABLE IF EXISTS branch_inventory;
ALTER TABLE medication_batches DROP CONSTRAINT IF EXISTS fk_batches_supplier;
DROP TABLE IF EXISTS suppliers;
