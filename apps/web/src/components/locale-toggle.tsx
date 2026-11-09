'use client';

import * as React from 'react';

import { ConfirmDialog, cn } from '@pharmaos/ui';

import { t } from '@/lib/i18n';
import { useLocaleStore } from '@/lib/locale-store';

/**
 * [ع | EN] language toggle (RC wave هـ). Switching remounts the whole client
 * tree (Providers wraps children in <Fragment key={locale}>) — open forms and
 * the POS cart are lost — so the switch always confirms first, in the CURRENT
 * language. The first render after a switch reads the new locale from the
 * cookie via the server layout, so no confirm is needed on that path.
 */
export function LocaleToggle({ className }: { className?: string }) {
  const locale = useLocaleStore((s) => s.locale);
  const setLocale = useLocaleStore((s) => s.setLocale);
  const [confirming, setConfirming] = React.useState<'en' | 'ar' | null>(null);

  const ask = (target: 'en' | 'ar') => {
    if (target === locale) return;
    setConfirming(target);
  };

  // Dense control height (--control-h-sm, 36px): a compact utility that also
  // sits in the 64px topbar next to the dense logout button.
  // Ring is inset because the group's overflow-hidden would clip an outer ring.
  // Callers may pass positioning classes (e.g. login card corner) — merged
  // over the chrome classes, never replacing them.
  const segBase =
    'flex h-[var(--control-h-sm)] items-center px-3 text-sm font-semibold transition-colors ' +
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset ' +
    'focus-visible:ring-primary-500';

  return (
    <>
      <div
        className={cn(
          'flex overflow-hidden rounded-[var(--radius-md)] border border-border',
          className,
        )}
        role="group"
        aria-label={t('locale.toggle')}
      >
        <button
          type="button"
          aria-pressed={locale === 'ar'}
          onClick={() => ask('ar')}
          className={`${segBase} ${
            locale === 'ar'
              ? 'bg-primary-600 text-white'
              : 'bg-white text-slate-600 hover:bg-primary-50'
          }`}
        >
          ع
        </button>
        <button
          type="button"
          aria-pressed={locale === 'en'}
          onClick={() => ask('en')}
          className={`${segBase} ${
            locale === 'en'
              ? 'bg-primary-600 text-white'
              : 'bg-white text-slate-600 hover:bg-primary-50'
          }`}
        >
          EN
        </button>
      </div>

      <ConfirmDialog
        open={confirming !== null}
        onClose={() => setConfirming(null)}
        onConfirm={() => {
          if (confirming) setLocale(confirming);
          setConfirming(null);
        }}
        title={t('locale.switch_title')}
        message={t('locale.switch_body')}
        confirmLabel={confirming === 'en' ? 'EN' : 'ع'}
        cancelLabel={t('users.cancel')}
        danger={false}
      />
    </>
  );
}
