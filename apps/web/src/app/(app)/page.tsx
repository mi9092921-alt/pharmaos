'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@pharmaos/ui';

import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { NAV_ITEMS } from '@/lib/nav';

/** Dashboard home. Quick tiles reflect the sections the role can reach; ones
 * still to be built are shown as "coming soon" so the IA is visible early. */
export default function DashboardHome() {
  const user = useAuth((s) => s.user);
  const hasPermission = useAuth((s) => s.hasPermission);
  const roleLabel = user?.role ? t(`role.${user.role}`) : '';

  const tiles = NAV_ITEMS.filter((item) => item.href !== '/' && hasPermission(item.permission));

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">
          {t('dashboard.welcome')}، {user?.full_name}
        </h1>
        <p className="text-sm text-slate-500">
          {t('dashboard.role')}: {roleLabel}
        </p>
      </div>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-slate-600">
          {t('dashboard.quick_actions')}
        </h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          {tiles.map((item) => (
            <Card key={item.href} className={item.ready ? '' : 'opacity-60'}>
              <CardHeader>
                <CardTitle className="text-base">{t(item.labelKey)}</CardTitle>
              </CardHeader>
              <CardContent className="pt-0 text-xs text-slate-500">
                {item.ready ? '' : t('dashboard.coming_soon')}
              </CardContent>
            </Card>
          ))}
        </div>
      </section>
    </div>
  );
}
