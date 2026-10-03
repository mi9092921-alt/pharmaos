/**
 * Generate packages/db/seeds/rbac_seed.sql from packages/shared/src/permissions.ts.
 *
 * The seed is IDEMPOTENT and enforces "code always wins" for built-in roles:
 *   - upserts the 6 system roles and every permission code
 *   - grants exactly the matrix pairs (reviving soft-deleted grants)
 *   - SOFT-deletes grants on system roles that are no longer in the matrix
 *     (never hard-deletes — CLAUDE.md forbidden action #1)
 *   - never touches custom roles (is_system = FALSE)
 *
 * Updates use guarded WHERE clauses so an unchanged seed run causes zero row
 * churn (no sync_version noise).
 *
 * Run (no build step — Node type stripping):
 *   node --experimental-strip-types packages/db/scripts/generate-rbac-seed.ts
 * CI verifies the committed seed is fresh via `git diff --exit-code`.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  PERMISSIONS,
  SYSTEM_ROLE_NAMES_AR,
  SystemRole,
  type SystemRoleCode,
} from '../../shared/src/permissions.ts';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const outPath = join(repoRoot, 'packages', 'db', 'seeds', 'rbac_seed.sql');

const roleCodes = Object.values(SystemRole) as SystemRoleCode[];
const permissionCodes = Object.keys(PERMISSIONS).sort();

const pairs: Array<[string, string]> = [];
for (const permission of permissionCodes) {
  for (const role of PERMISSIONS[permission] ?? []) {
    pairs.push([role, permission]);
  }
}
pairs.sort((a, b) => a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]));

const roleValues = roleCodes
  .map((code) => `    ('${code}', '${SYSTEM_ROLE_NAMES_AR[code]}')`)
  .join(',\n');
const permissionValues = permissionCodes.map((code) => `    ('${code}')`).join(',\n');
const pairValues = pairs.map(([r, p]) => `    ('${r}', '${p}')`).join(',\n');

const sql = `-- rbac_seed.sql — GENERATED FILE, DO NOT EDIT BY HAND.
-- Source of truth: packages/shared/src/permissions.ts
-- Regenerate: node --experimental-strip-types packages/db/scripts/generate-rbac-seed.ts
-- Applied after every migration run (code always wins over manual DB edits).

BEGIN;

-- 1) System roles (upsert; revive if soft-deleted; enforce name/is_system).
INSERT INTO roles (code, name_ar, is_system)
SELECT v.code, v.name_ar, TRUE
FROM (VALUES
${roleValues}
) AS v(code, name_ar)
ON CONFLICT (code) DO UPDATE
    SET name_ar = EXCLUDED.name_ar, is_system = TRUE, is_deleted = FALSE
    WHERE roles.name_ar IS DISTINCT FROM EXCLUDED.name_ar
       OR roles.is_system IS DISTINCT FROM TRUE
       OR roles.is_deleted IS DISTINCT FROM FALSE;

-- 2) Permission codes (upsert; revive if soft-deleted).
INSERT INTO permissions (code)
SELECT v.code
FROM (VALUES
${permissionValues}
) AS v(code)
ON CONFLICT (code) DO UPDATE
    SET is_deleted = FALSE
    WHERE permissions.is_deleted IS DISTINCT FROM FALSE;

-- 3) The matrix (role_code, permission_code) — exactly as defined in code.
CREATE TEMP TABLE _rbac_matrix (role_code TEXT NOT NULL, permission_code TEXT NOT NULL)
    ON COMMIT DROP;
INSERT INTO _rbac_matrix (role_code, permission_code) VALUES
${pairValues};

-- 4) Grant matrix pairs (insert missing; revive soft-deleted).
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _rbac_matrix m
JOIN roles r ON r.code = m.role_code
JOIN permissions p ON p.code = m.permission_code
ON CONFLICT (role_id, permission_id) DO UPDATE
    SET is_deleted = FALSE
    WHERE role_permissions.is_deleted IS DISTINCT FROM FALSE;

-- 5) Code wins: SOFT-delete grants on SYSTEM roles that are not in the matrix.
--    Custom roles (is_system = FALSE) are never touched.
UPDATE role_permissions rp
SET is_deleted = TRUE
FROM roles r, permissions p
WHERE rp.role_id = r.id
  AND rp.permission_id = p.id
  AND r.is_system
  AND NOT rp.is_deleted
  AND NOT EXISTS (
      SELECT 1 FROM _rbac_matrix m
      WHERE m.role_code = r.code AND m.permission_code = p.code
  );

COMMIT;
`;

mkdirSync(dirname(outPath), { recursive: true });
writeFileSync(outPath, sql, 'utf-8');
process.stdout.write(
  `rbac_seed.sql written: ${roleCodes.length} roles, ${permissionCodes.length} permissions, ${pairs.length} grants\n`,
);
