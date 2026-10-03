'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';

import { Button, Card, CardContent, CardHeader, CardTitle, Input, Label } from '@pharmaos/ui';

import { ApiRequestError, login } from '@/lib/api';
import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';

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

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="items-center text-center">
          <div className="text-3xl font-extrabold text-primary-600">{t('app.name')}</div>
          <CardTitle className="text-base font-medium text-slate-500">{t('app.tagline')}</CardTitle>
        </CardHeader>
        <CardContent>
          <h1 className="mb-4 text-lg font-bold">{t('login.title')}</h1>

          {/* Clear, translated error — never a technical message (UX rules). */}
          {errorCode && (
            <div
              role="alert"
              className="mb-4 rounded-[var(--radius-md)] border border-danger/30 bg-red-50 p-3 text-sm text-danger"
            >
              {t(`errors.${errorCode}`)}
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
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                aria-invalid={!!errors.password}
                {...register('password')}
              />
              {errors.password?.message && (
                <p className="text-xs text-danger">{t(errors.password.message)}</p>
              )}
            </div>

            {/* Loading state on every async operation (UX rules). */}
            <Button type="submit" size="lg" className="w-full" disabled={isSubmitting}>
              {isSubmitting ? t('login.submitting') : t('login.submit')}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
