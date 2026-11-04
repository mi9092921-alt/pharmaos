'use client';

import { hasPermission } from '@pharmaos/shared';
import { Icon, type IconName, cn } from '@pharmaos/ui';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { NAV_ITEMS } from '@/lib/nav';

/** Section grouping + icon per route (wave و). Group order is the visual
 * order; an item not listed keeps its NAV_ITEMS position at the end. */
const GROUPS: Array<{
  title: string;
  items: Array<{ href: string; icon: IconName }>;
}> = [
  {
    title: 'العمليات',
    items: [
      { href: '/pos', icon: 'pos' },
      { href: '/inventory', icon: 'inventory' },
      { href: '/stocktake', icon: 'stocktake' },
      { href: '/shortages', icon: 'shortages' },
    ],
  },
  {
    title: 'الفهرسة والمشتريات',
    items: [
      { href: '/catalog', icon: 'catalog' },
      { href: '/purchases', icon: 'purchases' },
    ],
  },
  {
    title: 'العملاء والمبيعات',
    items: [
      { href: '/customers', icon: 'customers' },
      { href: '/returns', icon: 'returns' },
      { href: '/prescriptions', icon: 'prescriptions' },
    ],
  },
  {
    title: 'المالية والامتثال',
    items: [
      { href: '/finance', icon: 'finance' },
      { href: '/compliance', icon: 'compliance' },
      { href: '/cashier', icon: 'cashier' },
    ],
  },
  {
    title: 'المتابعة والإدارة',
    items: [
      { href: '/', icon: 'dashboard' },
      { href: '/alerts', icon: 'alerts' },
      { href: '/notifications', icon: 'notifications' },
      { href: '/reports', icon: 'reports' },
      { href: '/users', icon: 'users' },
      { href: '/settings', icon: 'settings' },
      { href: '/about', icon: 'about' },
    ],
  },
];

const ICON_BY_HREF = new Map(GROUPS.flatMap((g) => g.items.map((it) => [it.href, it.icon])));
const GROUP_BY_HREF = new Map(GROUPS.flatMap((g) => g.items.map((it) => [it.href, g.title])));
const GROUP_ORDER = GROUPS.map((g) => g.title);

/**
 * Permission-driven navigation. Items the current role lacks permission for are
 * hidden; not-yet-built sections render disabled ("coming soon"). RTL-first:
 * the sidebar sits at the inline-start edge (right, under dir=rtl).
 */
export function Sidebar() {
  const pathname = usePathname();
  // Subscribe to the role value (not the stable store fn reference) so the
  // sidebar re-renders when /me resolves after a reload — otherwise `visible`
  // stays empty forever on the second visit (user starts as null).
  const role = useAuth((s) => s.user?.role);

  const visible = NAV_ITEMS.filter((item) => hasPermission(role, item.permission));

  return (
    <aside className="flex w-60 shrink-0 flex-col border-e border-border bg-white">
      <div className="flex h-16 items-center gap-2 border-b border-border px-5">
        <span className="text-xl font-extrabold text-primary-600">{t('app.name')}</span>
      </div>
      {/* min-h-0 lets the nav scroll internally when items exceed the
          viewport (aside height = shell h-screen stretch), same contract as
          <main> in the (app) layout. */}
      <nav className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {GROUP_ORDER.map((title) => {
          const items = visible.filter((item) => GROUP_BY_HREF.get(item.href) === title);
          if (items.length === 0) return null;
          return (
            <div key={title}>
              <p className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                {title}
              </p>
              <div className="space-y-1">
                {items.map((item) => {
                  const active = pathname === item.href;
                  const label = t(item.labelKey);
                  const icon = ICON_BY_HREF.get(item.href);
                  if (!item.ready) {
                    return (
                      <span
                        key={item.href}
                        aria-disabled
                        title={t('dashboard.coming_soon')}
                        className="flex cursor-not-allowed items-center gap-2 rounded-[var(--radius-md)] px-3 py-2 text-sm text-slate-400"
                      >
                        {icon && <Icon name={icon} size={15} className="opacity-60" />}
                        {label}
                        <span className="ms-auto text-[10px]">•</span>
                      </span>
                    );
                  }
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      aria-current={active ? 'page' : undefined}
                      className={cn(
                        'flex items-center gap-2 rounded-[var(--radius-md)] px-3 py-2 text-sm font-medium transition-colors',
                        active
                          ? 'bg-primary-50 text-primary-700'
                          : 'text-slate-700 hover:bg-primary-50',
                      )}
                    >
                      {icon && <Icon name={icon} size={15} />}
                      {label}
                    </Link>
                  );
                })}
              </div>
            </div>
          );
        })}
      </nav>
    </aside>
  );
}
