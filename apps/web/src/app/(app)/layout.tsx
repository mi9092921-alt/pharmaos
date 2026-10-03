'use client';

import { Spinner } from '@pharmaos/ui';
import { useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useEffect } from 'react';

import { Sidebar } from '@/components/sidebar';
import { Toaster } from '@/components/toaster';
import { Topbar } from '@/components/topbar';
import { ApiRequestError, fetchMe } from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';

/**
 * Authenticated shell. Bootstraps the session via /me and gates the dashboard:
 * unauthenticated users are redirected to /login. The shell itself works
 * offline once loaded (offline-first) — only the initial session check needs
 * the local API, which is on localhost.
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const setUser = useAuth((s) => s.setUser);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['me'],
    queryFn: fetchMe,
    retry: false,
  });

  useEffect(() => {
    if (data?.user) setUser(data.user);
  }, [data, setUser]);

  useEffect(() => {
    // Unauthorized -> back to login. Other errors keep the shell so the user
    // isn't kicked out on a transient blip while offline.
    if (isError && error instanceof ApiRequestError && error.code === 'E-AUTH-001') {
      router.replace('/login');
    }
  }, [isError, error, router]);

  if (isLoading || !data?.user) {
    return (
      <div className="flex min-h-screen items-center justify-center gap-3 text-slate-500">
        <Spinner />
        <span>{t('shell.loading')}</span>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
      <Toaster />
    </div>
  );
}
