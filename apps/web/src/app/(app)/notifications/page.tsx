'use client';

import { Badge, Button, Card, CardContent, Select, Spinner } from '@pharmaos/ui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import {
  listInventoryBranches,
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  type NotificationRow,
} from '@/lib/api';
import { t } from '@/lib/i18n';

const PRIORITY_TONE: Record<NotificationRow['priority'], 'danger' | 'warning' | 'primary'> = {
  critical: 'danger',
  high: 'danger',
  medium: 'warning',
  low: 'primary',
};

/** Render t(body_key) with {token} interpolation over params — the API ships
 * key + params, never localized strings (same contract as alerts). */
function renderBody(row: NotificationRow): string {
  const template = t(row.body_key);
  return template.replace(/\{(\w+)\}/g, (_, token: string) => {
    const value = row.params[token];
    return value === null || value === undefined ? '—' : String(value);
  });
}

/** Notification center (P3-M7) — the bell's destination. Unread rows carry
 * the primary-tinted edge; per-row and read-all marking are self-service on
 * VISIBLE rows (own + broadcast) behind notifications.view. */
export default function NotificationsPage() {
  const queryClient = useQueryClient();

  const [branchId, setBranchId] = useState('');
  const branchesQuery = useQuery({ queryKey: ['inv-branches'], queryFn: listInventoryBranches });
  const branches = branchesQuery.data ?? [];
  useEffect(() => {
    const first = branches[0];
    if (!branchId && first) setBranchId(first.id);
  }, [branches, branchId]);

  const listQuery = useQuery({
    queryKey: ['notifications', branchId],
    queryFn: () => listNotifications({ branchId, limit: 100 }),
    enabled: !!branchId,
    refetchInterval: 30_000,
  });

  const readMutation = useMutation({
    mutationFn: (id: string) => markNotificationRead({ branchId, notificationId: id }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notifications'] });
      queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
    },
  });
  const readAllMutation = useMutation({
    mutationFn: () => markAllNotificationsRead({ branchId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notifications'] });
      queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
    },
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

  const rows = listQuery.data?.notifications ?? [];
  const unreadCount = rows.filter((r) => !r.read_at).length;

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold text-slate-900">
          {t('notifications.title')}
          {unreadCount > 0 && (
            <span className="ms-3 align-middle text-sm font-semibold text-primary-600 tabular-nums">
              {t('notifications.unread').replace('{count}', String(unreadCount))}
            </span>
          )}
        </h1>
        <div className="flex items-center gap-2">
          {unreadCount > 0 && (
            <Button
              variant="outline"
              size="sm"
              disabled={readAllMutation.isPending}
              onClick={() => readAllMutation.mutate()}
            >
              {t('notifications.read_all')}
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

      {listQuery.isLoading ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : rows.length === 0 ? (
        <p className="py-10 text-center text-sm text-slate-500">{t('notifications.empty')}</p>
      ) : (
        <div className="space-y-3">
          {rows.map((row) => (
            <Card
              key={row.id}
              className={row.read_at ? 'opacity-70' : 'border-primary-200 bg-primary-50/40'}
            >
              <CardContent className="flex flex-wrap items-center gap-3 pt-4">
                <Badge tone={PRIORITY_TONE[row.priority]}>
                  {t(`notifications.priority_${row.priority}`)}
                </Badge>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-slate-900">{renderBody(row)}</p>
                  <p className="text-xs text-slate-400">
                    {new Date(row.created_at).toLocaleString()}
                    {row.channel === 'email' && !row.sent_at && (
                      <span className="ms-2">{t('notifications.pending_email')}</span>
                    )}
                  </p>
                </div>
                {!row.read_at && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={readMutation.isPending}
                    onClick={() => readMutation.mutate(row.id)}
                  >
                    {t('notifications.mark_read')}
                  </Button>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
