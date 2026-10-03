// Barrel for TypeScript/bundler consumers (apps/web, packages/ui). Extensionless
// so app tsconfigs resolve it without allowImportingTsExtensions. The Node
// strip-types RBAC generator imports ./permissions.ts directly (with its .ts
// extension), so it does not depend on this barrel.
export {
  ALL_ROLES,
  PERMISSIONS,
  SYSTEM_ROLE_NAMES_AR,
  SystemRole,
  hasPermission,
  type SystemRoleCode,
} from './permissions';
export { ERROR_CODES, type ApiResponse, type ErrorCode } from './errors';
