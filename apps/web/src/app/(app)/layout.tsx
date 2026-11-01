'use client';

import { Button, Card, CardContent, CardHeader, CardTitle, Spinner } from '@pharmaos/ui';
import { useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useEffect } from 'react';

import { Sidebar } from '@/components/sidebar';
import { Toaster } from '@/components/toaster';
import { Topbar } from '@/components/topbar';
import { LicenseUploadButton } from '@/components/license-upload';
import { ApiRequestError, fetchMe, getLicenseStatus, getSetupStatus } from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';

function errorCodeOf(error: unknown): string | null {
  return error instanceof ApiRequestError ? error.code : null;
}

/**
 * Authenticated shell. Bootstraps the session via /me and gates the dashboard:
 * unauthenticated users are redirected to /login. The shell itself works
 * offline once loaded (offline-first) — only the initial session check needs
 * the local API, which is on localhost.
 *
 * Boot failures NEVER hang on the spinner: every settled non-session state
 * renders an actionable screen (license activation with the device HWID, or a
 * generic error with a retry button).
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const setUser = useAuth((s) => s.setUser);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['me'],
    queryFn: fetchMe,
    retry: false,
  });
  const setupQuery = useQuery({
    queryKey: ['setup-status'],
    queryFn: getSetupStatus,
    enabled: !!data?.user,
    staleTime: 60_000,
  });
  // Live license gate (P4-M2): a session can outlive its license. The
  // cold-boot E-LIC gate below only fires when the FIRST /me fails, so a
  // license that degrades mid-session (tamper / key_lost / error /
  // unlicensed) used to leave every page rendered with 503 toasts (observed
  // live in the 2026-10-30 VM round). Poll the allowlisted status endpoint
  // and re-gate the whole shell. grace/read_only keep the app usable per the
  // licensing ladder; a failed poll (offline) keeps pages up — offline-first.
  const licenseQuery = useQuery({
    queryKey: ['license-status'],
    queryFn: getLicenseStatus,
    retry: false,
    refetchInterval: 30_000,
    enabled: !!data?.user,
    staleTime: 10_000,
  });
  const licenseOperational =
    licenseQuery.data === undefined ||
    ['active', 'grace', 'read_only'].includes(licenseQuery.data.status);

  useEffect(() => {
    if (data?.user) setUser(data.user);
  }, [data, setUser]);

  const code = errorCodeOf(error);

  useEffect(() => {
    // Unauthorized -> back to login. Other errors keep their own screen below
    // so the user isn't kicked out on a transient blip while offline.
    if (isError && code === 'E-AUTH-001') {
      router.replace('/login');
    }
  }, [isError, code, router]);

  if (isLoading || (isError && code === 'E-AUTH-001')) {
    return (
      <div className="flex min-h-screen items-center justify-center gap-3 text-slate-500">
        <Spinner />
        <span>{t('shell.loading')}</span>
      </div>
    );
  }

  // License gate (P4-M2): unlicensed / tamper / key_lost / clock_error / error
  // states answer /me with E-LIC-*. Show the activation screen with the device
  // HWID instead of hanging — the full /activation upload flow lands in P4-M3.
  if (isError && code !== null && code.startsWith('E-LIC-')) {
    return <LicenseRequiredScreen code={code} onRetry={() => void refetch()} />;
  }

  // Live re-gate: the license degraded while a session was already loaded.
  if (data?.user && !licenseOperational) {
    return (
      <LicenseRequiredScreen
        code="E-LIC-001"
        onRetry={() => {
          void licenseQuery.refetch();
          void refetch();
        }}
      />
    );
  }

  if (isError || !data?.user) {
    // API unreachable, 5xx, or any other boot failure — actionable error with
    // a retry button (CLAUDE.md UX rules: clear errors with resolution steps).
    return <BootErrorScreen onRetry={() => void refetch()} />;
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        {setupQuery.data && !setupQuery.data.backup_key_exported && (
          <div
            role="alert"
            className="mx-6 mt-4 rounded-[var(--radius-md)] border border-warning/50 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-900"
          >
            {t('shell.backup_key_warning')}
          </div>
        )}
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
      <Toaster />
    </div>
  );
}

function LicenseRequiredScreen({ code, onRetry }: { code: string; onRetry: () => void }) {
  // Allowlisted endpoint (no session) — the only source of the HWID that the
  // owner needs to issue the license file.
  const statusQuery = useQuery({
    queryKey: ['license-status'],
    queryFn: getLicenseStatus,
    retry: false,
  });
  const hwid = statusQuery.data?.hwid ?? null;

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="items-center text-center">
          <div className="text-3xl font-extrabold text-primary-600">{t('app.name')}</div>
          <CardTitle className="text-base font-medium text-slate-500">
            {t('shell.license_title')}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div
            role="alert"
            className="rounded-[var(--radius-md)] border border-warning/40 bg-amber-50 p-3 text-sm text-slate-700"
          >
            {t(`errors.${code}`)}
          </div>

          <div className="rounded-[var(--radius-md)] border border-border bg-surface p-3 text-center">
            <div className="text-xs text-slate-500">{t('shell.hwid')}</div>
            {statusQuery.isLoading ? (
              <div className="flex items-center justify-center gap-2 py-2 text-slate-500">
                <Spinner />
              </div>
            ) : hwid ? (
              <div
                className="mt-1 text-lg font-bold tracking-wider text-slate-800 tabular-nums"
                dir="ltr"
              >
                {hwid}
              </div>
            ) : (
              <div className="mt-1 text-sm text-danger">{t('shell.hwid_unavailable')}</div>
            )}
          </div>

          <ol className="list-none space-y-2 text-sm text-slate-600">
            <li>{t('shell.license_step1')}</li>
            <li>{t('shell.license_step2')}</li>
            <li>{t('shell.license_step3')}</li>
          </ol>

          <LicenseUploadButton
            className="w-full"
            onActivated={() => {
              void statusQuery.refetch();
              onRetry();
            }}
          />
          <Button
            size="lg"
            className="w-full"
            onClick={() => {
              void statusQuery.refetch();
              onRetry();
            }}
          >
            {t('shell.retry')}
          </Button>
        </CardContent>
      </Card>
    </main>
  );
}

function BootErrorScreen({ onRetry }: { onRetry: () => void }) {
  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="items-center text-center">
          <div className="text-3xl font-extrabold text-primary-600">{t('app.name')}</div>
          <CardTitle className="text-base font-medium text-slate-500">
            {t('shell.boot_error_title')}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div
            role="alert"
            className="rounded-[var(--radius-md)] border border-danger/30 bg-red-50 p-3 text-sm text-danger"
          >
            {t('shell.boot_error_body')}
          </div>
          <Button size="lg" className="w-full" onClick={onRetry}>
            {t('shell.retry')}
          </Button>
        </CardContent>
      </Card>
    </main>
  );
}
