'use client';

import * as React from 'react';

import { useAuth } from '@/lib/auth-store';
import { t } from '@/lib/i18n';

/**
 * Page-level permission guard (CLAUDE.md UX pattern, RC-A phase 3). Renders
 * children only when the current role holds ALL the given permissions — pass
 * an array for an ANY-of check (e.g. the three report tiers). UX only: the
 * backend remains authoritative for every request.
 */
export function RequirePermission({
  permission,
  children,
}: {
  permission: string | string[];
  children: React.ReactNode;
}) {
  const allowed = useAuth((s) => {
    const list = Array.isArray(permission) ? permission : [permission];
    return list.some((p) => s.hasPermission(p));
  });

  if (allowed) return <>{children}</>;

  return (
    <div className="mx-auto max-w-md space-y-3 py-16 text-center">
      <p className="text-3xl" role="img" aria-label={t('errors.E-AUTH-002')}>
        🔒
      </p>
      <p className="text-lg font-semibold text-slate-800">{t('errors.E-AUTH-002')}</p>
      <p className="text-sm text-slate-500">{t('require_permission.hint')}</p>
    </div>
  );
}
