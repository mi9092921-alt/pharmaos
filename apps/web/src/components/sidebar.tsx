'use client';

import { cn } from '@pharmaos/ui';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { NAV_ITEMS } from '@/lib/nav';

/**
 * Permission-driven navigation. Items the current role lacks permission for are
 * hidden; not-yet-built sections render disabled ("coming soon"). RTL-first:
 * the sidebar sits at the inline-start edge (right, under dir=rtl).
 */
export function Sidebar() {
  const pathname = usePathname();
  const hasPermission = useAuth((s) => s.hasPermission);

  const visible = NAV_ITEMS.filter((item) => hasPermission(item.permission));

  return (
    <aside className="flex w-60 shrink-0 flex-col border-e border-border bg-white">
      <div className="flex h-16 items-center gap-2 border-b border-border px-5">
        <span className="text-xl font-extrabold text-primary-600">{t('app.name')}</span>
      </div>
      <nav className="flex-1 space-y-1 overflow-y-auto p-3">
        {visible.map((item) => {
          const active = pathname === item.href;
          const label = t(item.labelKey);
          if (!item.ready) {
            return (
              <span
                key={item.href}
                aria-disabled
                title={t('dashboard.coming_soon')}
                className="flex cursor-not-allowed items-center justify-between rounded-[var(--radius-md)] px-3 py-2 text-sm text-slate-400"
              >
                {label}
                <span className="text-[10px]">•</span>
              </span>
            );
          }
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                'block rounded-[var(--radius-md)] px-3 py-2 text-sm font-medium transition-colors',
                active ? 'bg-primary-50 text-primary-700' : 'text-slate-700 hover:bg-primary-50',
              )}
            >
              {label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
