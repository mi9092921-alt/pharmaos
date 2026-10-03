-- 20260711000400_users_role.sql
-- Phase 0 / M6 — user -> role assignment.
--
-- Rationale: the CLAUDE.md Core table list is exactly
-- {branches, users, roles, permissions, role_permissions} — it defines NO
-- user_roles junction, so role assignment is a single role reference on the
-- user (one SystemRole per user), which the auth backbone requires in order to
-- authorize anything. Nullable: a user without a role has no permissions until
-- one is assigned.

ALTER TABLE users
    ADD COLUMN role_id UUID REFERENCES roles(id);

CREATE INDEX idx_users_role ON users(role_id) WHERE NOT is_deleted;
