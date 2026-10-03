/**
 * Dashboard navigation — each item declares the permission required to see it.
 * The sidebar filters items with hasPermission (packages/shared matrix), so a
 * cashier never sees Reports/Settings, etc. Backend guards remain authoritative;
 * this is UX only.
 *
 * Routes for sections not yet built (Phase 1 milestones M3+) are included but
 * flagged `ready: false` so the shell can render them as disabled "coming soon"
 * entries — the information architecture is stable from the start.
 */

export interface NavItem {
  href: string;
  labelKey: string;
  permission: string;
  ready: boolean;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { href: '/', labelKey: 'nav.dashboard', permission: 'sales.view', ready: true },
  { href: '/pos', labelKey: 'nav.pos', permission: 'sales.create', ready: true },
  { href: '/catalog', labelKey: 'nav.catalog', permission: 'inventory.view', ready: true },
  { href: '/inventory', labelKey: 'nav.inventory', permission: 'inventory.view', ready: true },
  { href: '/purchases', labelKey: 'nav.purchases', permission: 'purchases.view', ready: true },
  { href: '/customers', labelKey: 'nav.customers', permission: 'customers.view', ready: true },
  { href: '/returns', labelKey: 'nav.returns', permission: 'sales.view', ready: true },
  {
    href: '/prescriptions',
    labelKey: 'nav.prescriptions',
    permission: 'prescriptions.view',
    ready: true,
  },
  { href: '/finance', labelKey: 'nav.finance', permission: 'finance.expenses', ready: true },
  {
    href: '/compliance',
    labelKey: 'nav.compliance',
    permission: 'compliance.ereceipt',
    ready: true,
  },
  { href: '/cashier', labelKey: 'nav.cashier', permission: 'cashier.open_session', ready: true },
  { href: '/reports', labelKey: 'nav.reports', permission: 'reports.sales', ready: false },
  { href: '/users', labelKey: 'nav.users', permission: 'settings.users', ready: true },
  { href: '/settings', labelKey: 'nav.settings', permission: 'settings.view', ready: true },
];
