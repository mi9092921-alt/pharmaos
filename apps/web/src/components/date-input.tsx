'use client';

import { DateInput as UIDateInput, type DateInputProps } from '@pharmaos/ui';

import { useLocaleStore } from '@/lib/locale-store';

/**
 * Locale-aware passthrough over the shared DateInput (RC wave هـ): the
 * format hint/placeholder follows the active UI language. The input itself
 * stays an isolated LTR island in BOTH locales (ISO values are inherently
 * LTR) — only the surrounding hint text changes.
 */
export function DateInput(props: Omit<DateInputProps, 'locale'>) {
  const locale = useLocaleStore((s) => s.locale);
  return <UIDateInput {...props} locale={locale} />;
}

export {
  DateFormatHint,
  DATE_FORMAT_HINT_AR,
  DATE_FORMAT_HINT_LTR,
  LRM,
  bidiDatePlaceholder,
} from '@pharmaos/ui';
