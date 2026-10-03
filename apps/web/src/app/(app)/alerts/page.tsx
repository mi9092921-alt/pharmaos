'use client';

import { Badge, Button, Card, CardContent, Select, Spinner } from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import {
  acknowledgeAlert,
  type AlertRow,
  type AlertSeverity,
  evaluateAlerts,
  listAlerts,
  listInventoryBranches,
  resolveAlert,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';

const SEV_TONE: Record<AlertSeverity, 'warning' | 'danger'> = {
  warning: 'warning',
  danger: 'danger',
  critical: 'danger',
};

const STATUS_TABS = ['active', 'acknowledged', 'resolved', 'all'] as const;
type StatusFilter = (typeof STATUS_TABS)[number];

/** Render alerts.msg.<rule_key> with {token} interpolation over the alert's
 * params — the API ships key + params, never localized strings. */
function renderMessage(alert: AlertRow): string {
  const template = t(`alerts.msg.${alert.rule_key}`);
  return template.replace(/\{(\w+)\}/g, (_, token: string) => {
    const value = alert.params[token];
    return value === null || value === undefined ? '—' : String(value);
  });
}

export default function AlertsPage() {
  const canManage = useAuth((s) => s.hasPermission('alerts.manage'));
  const queryClient = useQueryClient();

  const [branchId, setBranchId] = useState('');
  const [status, setStatus] = useState<StatusFilter>('active');

  const branchesQuery = useQuery({ queryKey: ['inv-branches'], queryFn: listInventoryBranches });
  const branches = branchesQuery.data ?? [];
  useEffect(() => {
    const first = branches[0];
    if (!branchId && first) setBranchId(first.id);
  }, [branches, branchId]);

  const alertsQuery = useQuery({
    queryKey: ['alerts', branchId, status],
    queryFn: () => listAlerts({ branchId, status, limit: 100 }),
    enabled: !!branchId,
  });

  const ackMutation = useMutation({
    mutationFn: (id: string) => acknowledgeAlert(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alerts'] }),
  });
  const resolveMutation = useMutation({
    mutationFn: (id: string) => resolveAlert(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alerts'] }),
  });
  const evaluateMutation = useMutation({
    mutationFn: () => evaluateAlerts({ branchId }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alerts'] }),
  });

  if (branchesQuery.isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner />
      </div>
    );
  }
  if (branches.length === 0) {
    return <p className="py-10 text-center text-slate-500">{t('inventory.no_branch')}</p>;
  }

  const data = alertsQuery.data;
  const alerts = data?.alerts ?? [];

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">{t('alerts.title')}</h1>
        <div className="flex items-center gap-2">
          {canManage && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => evaluateMutation.mutate()}
              disabled={evaluateMutation.isPending}
            >
              {t('alerts.evaluate')}
            </Button>
          )}
          <Select className="h-9" value={branchId} onChange={(e) => setBranchId(e.target.value)}>
            {branches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </Select>
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        {STATUS_TABS.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setStatus(s)}
            className={
              'border-b-2 px-4 py-2 text-sm font-semibold transition-colors ' +
              (status === s
                ? 'border-primary-600 text-primary-700'
                : 'border-transparent text-slate-500')
            }
          >
            {t(`alerts.tab_${s}`)}
          </button>
        ))}
      </div>

      {alertsQuery.isLoading ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : alerts.length === 0 ? (
        <p className="py-10 text-center text-sm text-slate-500">{t('alerts.empty')}</p>
      ) : (
        <div className="space-y-3">
          {alerts.map((alert) => (
            <Card key={alert.id}>
              <CardContent className="flex flex-wrap items-center gap-3 pt-4">
                <Badge tone={SEV_TONE[alert.severity as AlertSeverity]}>
                  {t(`alerts.severity_${alert.severity}`)}
                </Badge>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-slate-900">
                    {t(`alerts.rule_${alert.rule_key}`)}
                  </p>
                  <p className="truncate text-xs text-slate-500">{renderMessage(alert)}</p>
                  <p className="text-xs text-slate-400">
                    {t('alerts.last_seen')}: {new Date(alert.last_seen).toLocaleString()}
                  </p>
                </div>
                {alert.status !== 'resolved' && canManage && (
                  <div className="flex gap-2">
                    {alert.status === 'active' && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={ackMutation.isPending}
                        onClick={() => ackMutation.mutate(alert.id)}
                      >
                        {t('alerts.ack')}
                      </Button>
                    )}
                    <Button
                      size="sm"
                      disabled={resolveMutation.isPending}
                      onClick={() => resolveMutation.mutate(alert.id)}
                    >
                      {t('alerts.resolve')}
                    </Button>
                  </div>
                )}
                {alert.status !== 'active' && (
                  <Badge tone={alert.status === 'resolved' ? 'success' : 'warning'}>
                    {t(`alerts.status_${alert.status}`)}
                  </Badge>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
