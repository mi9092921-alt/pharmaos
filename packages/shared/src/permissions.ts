/**
 * PharmaOS RBAC — THE single source of truth for the permission matrix
 * (CLAUDE.md v1.1: "مصفوفة الصلاحيات تُعرَّف في الكود").
 *
 * - roles/permissions tables in the DB are SEEDED from this file on every
 *   migration run (packages/db/scripts/generate-rbac-seed.ts).
 * - Manual edits to built-in role permissions in the DB are overwritten —
 *   code always wins.
 * - Custom roles are created in the DB ON TOP of the permission codes defined
 *   here (is_system = FALSE; the seeder never touches them).
 *
 * Note: implemented with erasable-only TypeScript syntax (const objects, not
 * TS enums) so the seeder runs directly under Node type-stripping with zero
 * build step. Codes and members match CLAUDE.md exactly.
 */

export const SystemRole = {
  SUPER_ADMIN: 'super_admin', // مالك النظام — كل الصلاحيات
  BRANCH_MANAGER: 'branch_manager', // مدير الفرع
  PHARMACIST: 'pharmacist', // صيدلاني
  CASHIER: 'cashier', // كاشير
  DATA_ENTRY: 'data_entry', // مدخل بيانات
  VIEWER: 'viewer', // مشاهدة فقط
} as const;

export type SystemRoleCode = (typeof SystemRole)[keyof typeof SystemRole];

/** Arabic display names for the built-in roles (from CLAUDE.md). */
export const SYSTEM_ROLE_NAMES_AR: Record<SystemRoleCode, string> = {
  super_admin: 'مالك النظام',
  branch_manager: 'مدير الفرع',
  pharmacist: 'صيدلاني',
  cashier: 'كاشير',
  data_entry: 'مدخل بيانات',
  viewer: 'مشاهدة فقط',
};

export const ALL_ROLES: readonly SystemRoleCode[] = [
  SystemRole.SUPER_ADMIN,
  SystemRole.BRANCH_MANAGER,
  SystemRole.PHARMACIST,
  SystemRole.CASHIER,
  SystemRole.DATA_ENTRY,
  SystemRole.VIEWER,
];

/** The detailed permission matrix — transcribed exactly from CLAUDE.md. */
export const PERMISSIONS: Record<string, readonly SystemRoleCode[]> = {
  // =================== المخزون ===================
  'inventory.view': ALL_ROLES,
  'inventory.add': ['super_admin', 'branch_manager', 'pharmacist', 'data_entry'],
  'inventory.edit': ['super_admin', 'branch_manager', 'pharmacist'],
  'inventory.delete': ['super_admin', 'branch_manager'],
  'inventory.adjust': ['super_admin', 'branch_manager', 'pharmacist'],
  'inventory.purchase': ['super_admin', 'branch_manager'],

  // =================== المبيعات ===================
  'sales.view': ALL_ROLES,
  'sales.create': ['super_admin', 'branch_manager', 'pharmacist', 'cashier'],
  'sales.cancel': ['super_admin', 'branch_manager'],
  'sales.discount': ['super_admin', 'branch_manager', 'pharmacist'],
  'sales.return': ['super_admin', 'branch_manager', 'pharmacist'],
  'sales.override_price': ['super_admin', 'branch_manager'],

  // =================== العملاء ===================
  'customers.view': ALL_ROLES,
  'customers.create': ['super_admin', 'branch_manager', 'pharmacist', 'cashier'],
  'customers.edit': ['super_admin', 'branch_manager', 'pharmacist'],
  'customers.delete': ['super_admin'],

  // =================== التقارير ===================
  'reports.sales': ['super_admin', 'branch_manager'],
  'reports.inventory': ['super_admin', 'branch_manager', 'pharmacist'],
  'reports.financial': ['super_admin', 'branch_manager'],
  'reports.audit': ['super_admin'],
  'reports.export': ['super_admin', 'branch_manager'],

  // =================== الإعدادات ===================
  'settings.view': ['super_admin', 'branch_manager'],
  'settings.edit': ['super_admin'],
  'settings.users': ['super_admin'],
  'settings.backup': ['super_admin'],

  // =================== الكاشير ===================
  'cashier.open_session': ['super_admin', 'branch_manager', 'cashier'],
  'cashier.close_session': ['super_admin', 'branch_manager'],
  'cashier.view_cash': ['super_admin', 'branch_manager'],

  // =================== المشتريات ===================
  'purchases.view': ['super_admin', 'branch_manager', 'pharmacist'],
  'purchases.create': ['super_admin', 'branch_manager'],
  'purchases.approve': ['super_admin', 'branch_manager'],
  'purchases.receive': ['super_admin', 'branch_manager', 'pharmacist'],

  // =================== المالية ===================
  'finance.expenses': ['super_admin', 'branch_manager'],
  'finance.reports': ['super_admin'],

  // =================== الامتثال (v1.1) ===================
  'compliance.ereceipt': ['super_admin', 'branch_manager'],
  'compliance.tt_report': ['super_admin', 'branch_manager', 'pharmacist'],

  // =================== الوصفات الطبية (v1.1) ===================
  'prescriptions.view': ['super_admin', 'branch_manager', 'pharmacist'],
  'prescriptions.create': ['super_admin', 'branch_manager', 'pharmacist'],
  'prescriptions.edit': ['super_admin', 'branch_manager', 'pharmacist'],

  // =================== سجل المواد الخاضعة للرقابة (v1.1) ===================
  // Written automatically by the sale flow — no manual create/edit permission.
  'controlled_substances.view': ['super_admin', 'branch_manager', 'pharmacist'],
};

/** Frontend guard helper — UX only; backend checks are authoritative. */
export function hasPermission(roleCode: string | null | undefined, permission: string): boolean {
  if (!roleCode) return false;
  const allowed = PERMISSIONS[permission];
  return allowed !== undefined && (allowed as readonly string[]).includes(roleCode);
}
