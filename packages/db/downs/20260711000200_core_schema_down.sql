-- Down migration for 20260711000200_core_schema.sql
-- Drop order is strict reverse-dependency: FK holders first, users last
-- (every table's created_by/updated_by references users).

DROP TABLE IF EXISTS role_permissions;
DROP TABLE IF EXISTS permissions;
DROP TABLE IF EXISTS roles;
DROP TABLE IF EXISTS branches;
DROP TABLE IF EXISTS countries;
DROP TABLE IF EXISTS tax_profiles;
DROP TABLE IF EXISTS currencies;
DROP TABLE IF EXISTS users;
DROP FUNCTION IF EXISTS touch_row();
