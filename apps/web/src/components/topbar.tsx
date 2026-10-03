'use client';

import { Badge, Button } from '@pharmaos/ui';
import { useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useEffect, useRef } from 'react';

import {
  getUnreadCount,
  listInventoryBranches,
  listNotifications,
  logout as apiLogout,
} from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { useOnline } from '@/lib/use-online';

/** Polls the branch's newest rows and fires an OS toast (Notification API —
 * native on the Electron desktop shell, permission-gated in browsers) for
 * NEW desktop-channel rows. First load only seeds the seen-set: history never
 * replays as toasts. */
function useDesktopToasts(branchId: string | undefined, enabled: boolean) {
  const seenIds = useRef<Set<string> | null>(null);
  const rowsQuery = useQuery({
    queryKey: ['notifications-desktop-feed', branchId],
    queryFn: () => listNotifications({ branchId: branchId as string, limit: 10 }),
    enabled: enabled && !!branchId,
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (!enabled || typeof window === 'undefined' || typeof Notification === 'undefined') return;
    const rows = rowsQuery.data?.notifications ?? [];
    if (seenIds.current === null) {
      seenIds.current = new Set(rows.map((r) => r.id));
      return;
    }
    for (const row of rows) {
      if (seenIds.current.has(row.id) || row.channel !== 'desktop') continue;
      seenIds.current.add(row.id);
      const body = t(row.body_key).replace(/\{(\w+)\}/g, (_, token: string) =>
        String(row.params[token] ?? '—'),
      );
      if (Notification.permission === 'granted') {
        new Notification(t(row.title_key), { body });
      }
    }
  }, [rowsQuery.data, enabled]);
}

/** Top bar: persistent online/offline status + notification bell (P3-M7) +
 * current user/role + sign out. The bell polls the unread counter every 30s
 * (TanStack Query) for viewers holding notifications.view; it links to the
 * /notifications center. */
export function Topbar() {
  const router = useRouter();
  const online = useOnline();
  const user = useAuth((s) => s.user);
  const setUser = useAuth((s) => s.setUser);
  const canNotifications = useAuth((s) => s.hasPermission('notifications.view'));

  const roleLabel = user?.role ? t(`role.${user.role}`) : '';

  const branchesQuery = useQuery({
    queryKey: ['inv-branches'],
    queryFn: listInventoryBranches,
    enabled: canNotifications,
  });
  const firstBranch = branchesQuery.data?.[0]?.id;
  const unreadQuery = useQuery({
    queryKey: ['notifications-unread', firstBranch],
    queryFn: () => getUnreadCount({ branchId: firstBranch as string }),
    enabled: !!firstBranch,
    refetchInterval: 30_000,
  });
  const unread = unreadQuery.data?.unread ?? 0;
  useDesktopToasts(firstBranch, canNotifications);

  const onLogout = async () => {
    try {
      await apiLogout();
    } finally {
      setUser(null);
      router.replace('/login');
    }
  };

  return (
    <header className="flex h-16 items-center justify-between border-b border-border bg-white px-6">
      <Badge tone={online ? 'success' : 'warning'}>
        <span
          className={`size-2 rounded-full ${online ? 'bg-success' : 'bg-warning'}`}
          aria-hidden
        />
        {online ? t('shell.online') : t('shell.offline')}
      </Badge>

      <div className="flex items-center gap-4">
        {canNotifications && (
          <button
            type="button"
            aria-label={t('notifications.title')}
            onClick={() => router.push('/notifications')}
            className="relative rounded-[var(--radius-md)] p-2 hover:bg-primary-50"
          >
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="text-slate-600"
              aria-hidden
            >
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
              <path d="M13.73 21a2 2 0 0 1-3.46 0" />
            </svg>
            {unread > 0 && (
              <span className="absolute -top-0.5 -end-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[10px] font-bold text-white tabular-nums">
                {unread > 99 ? '99+' : unread}
              </span>
            )}
          </button>
        )}
        <div className="text-end">
          <div className="text-sm font-semibold text-slate-800">{user?.full_name}</div>
          <div className="text-xs text-slate-500">{roleLabel}</div>
        </div>
        <Button variant="outline" size="sm" onClick={onLogout}>
          {t('shell.logout')}
        </Button>
      </div>
    </header>
  );
}
