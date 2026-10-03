'use client';

import { Badge, Button } from '@pharmaos/ui';
import { useRouter } from 'next/navigation';

import { logout as apiLogout } from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';
import { useOnline } from '@/lib/use-online';

/** Top bar: persistent online/offline status + current user/role + sign out. */
export function Topbar() {
  const router = useRouter();
  const online = useOnline();
  const user = useAuth((s) => s.user);
  const setUser = useAuth((s) => s.setUser);

  const roleLabel = user?.role ? t(`role.${user.role}`) : '';

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
