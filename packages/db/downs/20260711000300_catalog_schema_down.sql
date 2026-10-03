-- Down migration for 20260711000300_catalog_schema.sql
-- Reverse-dependency order; indexes fall with their tables.

DROP TABLE IF EXISTS medication_barcodes;
DROP TABLE IF EXISTS medication_packaging;
DROP TABLE IF EXISTS medications;
DROP TABLE IF EXISTS units;
DROP TABLE IF EXISTS categories;
DROP FUNCTION IF EXISTS normalize_arabic(TEXT);
DROP TEXT SEARCH CONFIGURATION IF EXISTS arabic_simple;
