'use client';

import { Badge, Card, CardContent, CardHeader, CardTitle, Spinner } from '@pharmaos/ui';
import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';

import { getAlertsSummary } from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { NAV_ITEMS } from '@/lib/nav';

/** Dashboard home. Quick tiles reflect the sections the role can reach; ones
 * still to be built are shown as "coming soon" so the IA is visible early.
 * The alerts banner (P3-M6) shows only when LIVE critical/danger alerts exist
 * — warnings live on /alerts and never shout over the dashboard. Since P3-M8
 * it watches ALL branches (the summary endpoint's rollup), so a second
 * branch's emergency can never hide behind a first-branch-only query. */
export default function DashboardHome() {
  const user = useAuth((s) => s.user);
  const hasPermission = useAuth((s) => s.hasPermission);
  const roleLabel = user?.role ? t(`role.${user.role}`) : '';

  const tiles = NAV_ITEMS.filter((item) => item.href !== '/' && hasPermission(item.permission));

  const canAlerts = hasPermission('alerts.view');
  const summaryQuery = useQuery({
    queryKey: ['alerts-summary'],
    queryFn: () => getAlertsSummary({}),
    enabled: canAlerts,
  });
  const urgent = (summaryQuery.data?.critical ?? 0) + (summaryQuery.data?.danger ?? 0);

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

      {canAlerts && summaryQuery.isLoading && (
        <div className="flex justify-center py-2">
          <Spinner />
        </div>
      )}
      {canAlerts && urgent > 0 && (
        <Link href="/alerts" className="block">
          <div className="flex items-center gap-3 rounded-[var(--radius-md)] border border-danger/30 bg-red-50 px-4 py-3">
            <Badge tone="danger">{t('alerts.severity_critical')}</Badge>
            <span className="text-sm font-medium text-danger">
              {t('alerts.banner_urgent').replace('{count}', String(urgent))}
            </span>
            <span className="ms-auto text-xs font-medium text-danger underline">
              {t('alerts.title')}
            </span>
          </div>
        </Link>
      )}

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
