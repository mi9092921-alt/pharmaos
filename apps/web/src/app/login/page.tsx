'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import Image from 'next/image';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';

import { Button, Card, CardContent, CardHeader, Icon, Input, Label, Spinner } from '@pharmaos/ui';

import { LocaleToggle } from '@/components/locale-toggle';
import { ApiRequestError, getLicenseStatus, login } from '@/lib/api';
import { LicenseUploadButton, licenseStatusLabel } from '@/components/license-upload';
import { useAuth } from '@/lib/auth-store';
import { localizedErrorMessage, t } from '@/lib/i18n';

// Form validation (React Hook Form + Zod — CLAUDE.md stack).
const loginSchema = z.object({
  username: z.string().min(1, 'validation.username_required'),
  password: z.string().min(1, 'validation.password_required'),
});
type LoginForm = z.infer<typeof loginSchema>;

export default function LoginPage() {
  const router = useRouter();
  const setUser = useAuth((s) => s.setUser);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [showPassword, setShowPassword] = useState(false);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginForm>({ resolver: zodResolver(loginSchema) });

  const onSubmit = async (values: LoginForm) => {
    setErrorCode(null);
    try {
      const data = await login(values.username, values.password);
      setUser(data.user);
      router.replace('/');
    } catch (error) {
      // API returns stable codes; the UI translates them (CLAUDE.md error rules).
      setErrorCode(error instanceof ApiRequestError ? error.code : 'E-SYS-001');
    }
  };

  // Device license watch (allowlisted — no session needed): surface an
  // activation box only when attention is required, otherwise stay out of
  // the way. A fresh activation refetches and the box disappears.
  const licenseQuery = useQuery({
    queryKey: ['license-status'],
    queryFn: getLicenseStatus,
    retry: false,
  });
  const lic = licenseQuery.data;
  const licenseAttention = !!lic && (lic.status !== 'active' || (lic.days_left ?? 999) <= 14);
  // Severity for the login box: read-only/expired/tampered states are blocking
  // (role="alert"); 'active yet expiring' and grace are informative
  // (role="status") — licenseStatusTone('active') is success, right for the
  // settings table, not for a warning box here.
  const licenseDanger =
    !!lic && ['read_only', 'tamper', 'clock_error', 'key_lost', 'error'].includes(lic.status);
  const keystoreUnavailable = lic?.diagnostic === 'device_keystore_unavailable';

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="relative w-full max-w-sm">
        {/* Language toggle parked in the card corner — the header stays one
            centered brand block and the form reads top-to-bottom. */}
        <LocaleToggle className="absolute end-3 top-3" />
        <CardHeader className="items-center text-center">
          <Image
            src="/brand/pharmaos-lockup.svg"
            alt={t('app.name')}
            width={220}
            height={51}
            priority
          />
          {/* <p>, not CardTitle (<h2>): the h1 below must stay the page's first
              and only heading — an h2 before h1 breaks the document outline. */}
          <p className="text-base font-medium text-slate-500">{t('app.tagline')}</p>
        </CardHeader>
        <CardContent>
          <h1 className="mb-4 text-center text-lg font-bold">{t('login.title')}</h1>

          {/* Clear, translated error — never a technical message (UX rules). */}
          {errorCode && (
            <div
              role="alert"
              className="mb-4 rounded-[var(--radius-md)] border border-danger/30 bg-red-50 p-3 text-sm text-danger"
            >
              {localizedErrorMessage(errorCode, 'حدث خطأ أثناء تسجيل الدخول — حاول مجدداً')}
            </div>
          )}

          {/* License activation — visible only when the device license needs
              attention (unlicensed / grace / expiring soon). Color and aria
              role follow severity: blocking states alert, expiring states
              whisper — and the days-left figure is shown so the reason is
              never a mystery. */}
          {licenseAttention && lic && (
            <div
              role={licenseDanger ? 'alert' : 'status'}
              className={`mb-4 space-y-2 rounded-[var(--radius-md)] border p-3 text-sm text-slate-700 ${
                licenseDanger ? 'border-danger/40 bg-red-50' : 'border-warning/70 bg-amber-50'
              }`}
            >
              <div className="font-medium">
                {t('settings.software_license.title')}:{' '}
                {keystoreUnavailable
                  ? localizedErrorMessage(
                      'E-LIC-009',
                      'مفاتيح أمان الجهاز غير متاحة — استعد مفاتيح الجهاز أولاً قبل تفعيل الترخيص',
                    )
                  : licenseStatusLabel(lic.status)}
              </div>
              {lic.days_left != null && lic.days_left <= 14 && (
                <div>{t('login.license_days_left', { days: lic.days_left })}</div>
              )}
              {keystoreUnavailable ? (
                <div className="rounded-[var(--radius-sm)] border border-danger/30 bg-red-50 p-2 text-xs text-danger">
                  {t('shell.keystore_recovery')}
                </div>
              ) : (
                <LicenseUploadButton
                  className="w-full"
                  onActivated={() => void licenseQuery.refetch()}
                />
              )}
            </div>
          )}

          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
            <div className="space-y-1.5">
              <Label htmlFor="username">{t('login.username')}</Label>
              <Input
                id="username"
                autoComplete="username"
                autoFocus
                aria-invalid={!!errors.username}
                {...register('username')}
              />
              {errors.username?.message && (
                <p className="text-xs text-danger">{t(errors.username.message)}</p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="password">{t('login.password')}</Label>
              <div className="relative">
                <Input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  aria-invalid={!!errors.password}
                  className="pe-10"
                  {...register('password')}
                />
                {/* Visibility toggle — type="button" so it never submits the form. */}
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={t(showPassword ? 'login.hide_password' : 'login.show_password')}
                  aria-pressed={showPassword}
                  className="absolute inset-y-0 end-0.5 my-auto flex h-9 w-9 items-center justify-center rounded-[var(--radius-sm)] text-slate-500 hover:text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
                >
                  <Icon name={showPassword ? 'eye_off' : 'eye'} size={16} />
                </button>
              </div>
              {errors.password?.message && (
                <p className="text-xs text-danger">{t(errors.password.message)}</p>
              )}
            </div>

            {/* Loading state on every async operation (UX rules). */}
            <Button type="submit" size="lg" className="w-full" disabled={isSubmitting}>
              {isSubmitting ? (
                <>
                  <Spinner className="size-4 border-white/70" aria-hidden />
                  {t('login.submitting')}
                </>
              ) : (
                t('login.submit')
              )}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
