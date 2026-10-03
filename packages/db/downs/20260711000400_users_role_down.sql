-- Down migration for 20260711000400_users_role.sql

DROP INDEX IF EXISTS idx_users_role;
ALTER TABLE users DROP COLUMN IF EXISTS role_id;
