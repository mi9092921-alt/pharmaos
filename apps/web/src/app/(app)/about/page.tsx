'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@pharmaos/ui';

import { t } from '@/lib/i18n';

export default function AboutPage() {
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">{t('about.title')}</h1>
        <p className="text-sm text-slate-500">{t('about.subtitle')}</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{t('about.scope_title')}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm leading-7 text-slate-700">
          <p>{t('about.scope_body')}</p>
          <p className="rounded-[var(--radius-md)] border border-warning/40 bg-amber-50 p-3 font-semibold">
            {t('about.limitations')}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
