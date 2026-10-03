-- rbac_seed.sql — GENERATED FILE, DO NOT EDIT BY HAND.
-- Source of truth: packages/shared/src/permissions.ts
-- Regenerate: node --experimental-strip-types packages/db/scripts/generate-rbac-seed.ts
-- Applied after every migration run (code always wins over manual DB edits).

BEGIN;

-- 1) System roles (upsert; revive if soft-deleted; enforce name/is_system).
INSERT INTO roles (code, name_ar, is_system)
SELECT v.code, v.name_ar, TRUE
FROM (VALUES
    ('super_admin', 'مالك النظام'),
    ('branch_manager', 'مدير الفرع'),
    ('pharmacist', 'صيدلاني'),
    ('cashier', 'كاشير'),
    ('data_entry', 'مدخل بيانات'),
    ('viewer', 'مشاهدة فقط')
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
    ('cashier.close_session'),
    ('cashier.open_session'),
    ('cashier.view_cash'),
    ('compliance.ereceipt'),
    ('compliance.tt_report'),
    ('controlled_substances.view'),
    ('customers.create'),
    ('customers.delete'),
    ('customers.edit'),
    ('customers.view'),
    ('finance.expenses'),
    ('finance.reports'),
    ('inventory.add'),
    ('inventory.adjust'),
    ('inventory.delete'),
    ('inventory.edit'),
    ('inventory.purchase'),
    ('inventory.view'),
    ('prescriptions.create'),
    ('prescriptions.edit'),
    ('prescriptions.view'),
    ('purchases.approve'),
    ('purchases.create'),
    ('purchases.receive'),
    ('purchases.view'),
    ('reports.audit'),
    ('reports.export'),
    ('reports.financial'),
    ('reports.inventory'),
    ('reports.sales'),
    ('sales.cancel'),
    ('sales.create'),
    ('sales.discount'),
    ('sales.override_price'),
    ('sales.return'),
    ('sales.view'),
    ('settings.backup'),
    ('settings.edit'),
    ('settings.users'),
    ('settings.view')
) AS v(code)
ON CONFLICT (code) DO UPDATE
    SET is_deleted = FALSE
    WHERE permissions.is_deleted IS DISTINCT FROM FALSE;

-- 3) The matrix (role_code, permission_code) — exactly as defined in code.
CREATE TEMP TABLE _rbac_matrix (role_code TEXT NOT NULL, permission_code TEXT NOT NULL)
    ON COMMIT DROP;
INSERT INTO _rbac_matrix (role_code, permission_code) VALUES
    ('branch_manager', 'cashier.close_session'),
    ('branch_manager', 'cashier.open_session'),
    ('branch_manager', 'cashier.view_cash'),
    ('branch_manager', 'compliance.ereceipt'),
    ('branch_manager', 'compliance.tt_report'),
    ('branch_manager', 'controlled_substances.view'),
    ('branch_manager', 'customers.create'),
    ('branch_manager', 'customers.edit'),
    ('branch_manager', 'customers.view'),
    ('branch_manager', 'finance.expenses'),
    ('branch_manager', 'inventory.add'),
    ('branch_manager', 'inventory.adjust'),
    ('branch_manager', 'inventory.delete'),
    ('branch_manager', 'inventory.edit'),
    ('branch_manager', 'inventory.purchase'),
    ('branch_manager', 'inventory.view'),
    ('branch_manager', 'prescriptions.create'),
    ('branch_manager', 'prescriptions.edit'),
    ('branch_manager', 'prescriptions.view'),
    ('branch_manager', 'purchases.approve'),
    ('branch_manager', 'purchases.create'),
    ('branch_manager', 'purchases.receive'),
    ('branch_manager', 'purchases.view'),
    ('branch_manager', 'reports.export'),
    ('branch_manager', 'reports.financial'),
    ('branch_manager', 'reports.inventory'),
    ('branch_manager', 'reports.sales'),
    ('branch_manager', 'sales.cancel'),
    ('branch_manager', 'sales.create'),
    ('branch_manager', 'sales.discount'),
    ('branch_manager', 'sales.override_price'),
    ('branch_manager', 'sales.return'),
    ('branch_manager', 'sales.view'),
    ('branch_manager', 'settings.view'),
    ('cashier', 'cashier.open_session'),
    ('cashier', 'customers.create'),
    ('cashier', 'customers.view'),
    ('cashier', 'inventory.view'),
    ('cashier', 'sales.create'),
    ('cashier', 'sales.view'),
    ('data_entry', 'customers.view'),
    ('data_entry', 'inventory.add'),
    ('data_entry', 'inventory.view'),
    ('data_entry', 'sales.view'),
    ('pharmacist', 'compliance.tt_report'),
    ('pharmacist', 'controlled_substances.view'),
    ('pharmacist', 'customers.create'),
    ('pharmacist', 'customers.edit'),
    ('pharmacist', 'customers.view'),
    ('pharmacist', 'inventory.add'),
    ('pharmacist', 'inventory.adjust'),
    ('pharmacist', 'inventory.edit'),
    ('pharmacist', 'inventory.view'),
    ('pharmacist', 'prescriptions.create'),
    ('pharmacist', 'prescriptions.edit'),
    ('pharmacist', 'prescriptions.view'),
    ('pharmacist', 'purchases.receive'),
    ('pharmacist', 'purchases.view'),
    ('pharmacist', 'reports.inventory'),
    ('pharmacist', 'sales.create'),
    ('pharmacist', 'sales.discount'),
    ('pharmacist', 'sales.return'),
    ('pharmacist', 'sales.view'),
    ('super_admin', 'cashier.close_session'),
    ('super_admin', 'cashier.open_session'),
    ('super_admin', 'cashier.view_cash'),
    ('super_admin', 'compliance.ereceipt'),
    ('super_admin', 'compliance.tt_report'),
    ('super_admin', 'controlled_substances.view'),
    ('super_admin', 'customers.create'),
    ('super_admin', 'customers.delete'),
    ('super_admin', 'customers.edit'),
    ('super_admin', 'customers.view'),
    ('super_admin', 'finance.expenses'),
    ('super_admin', 'finance.reports'),
    ('super_admin', 'inventory.add'),
    ('super_admin', 'inventory.adjust'),
    ('super_admin', 'inventory.delete'),
    ('super_admin', 'inventory.edit'),
    ('super_admin', 'inventory.purchase'),
    ('super_admin', 'inventory.view'),
    ('super_admin', 'prescriptions.create'),
    ('super_admin', 'prescriptions.edit'),
    ('super_admin', 'prescriptions.view'),
    ('super_admin', 'purchases.approve'),
    ('super_admin', 'purchases.create'),
    ('super_admin', 'purchases.receive'),
    ('super_admin', 'purchases.view'),
    ('super_admin', 'reports.audit'),
    ('super_admin', 'reports.export'),
    ('super_admin', 'reports.financial'),
    ('super_admin', 'reports.inventory'),
    ('super_admin', 'reports.sales'),
    ('super_admin', 'sales.cancel'),
    ('super_admin', 'sales.create'),
    ('super_admin', 'sales.discount'),
    ('super_admin', 'sales.override_price'),
    ('super_admin', 'sales.return'),
    ('super_admin', 'sales.view'),
    ('super_admin', 'settings.backup'),
    ('super_admin', 'settings.edit'),
    ('super_admin', 'settings.users'),
    ('super_admin', 'settings.view'),
    ('viewer', 'customers.view'),
    ('viewer', 'inventory.view'),
    ('viewer', 'sales.view');

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
