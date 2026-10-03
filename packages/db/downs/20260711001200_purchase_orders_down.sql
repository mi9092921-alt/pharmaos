-- Down migration for 20260711001200_purchase_orders.sql
-- Drop items first (FK -> purchase_orders). Triggers/indexes drop with the tables.
DROP TABLE IF EXISTS purchase_items;
DROP TABLE IF EXISTS purchase_orders;
