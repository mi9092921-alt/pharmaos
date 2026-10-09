import * as React from 'react';

import { cn } from './cn';

/**
 * BiDi-safe date input (PharmaOS UX — Arabic RTL-first).
 *
 * Problem: inside `dir="rtl"` containers the Unicode Bidirectional Algorithm
 * reorders neutral characters (`/`, `-`) mixed with Latin format tokens
 * (`yyyy/mm/dd`) and Arabic words, so placeholders/hints render corrupted
 * (e.g. `yyyy/رهش/موى` instead of `yyyy/mm/dd`).
 *
 * Fix applied here (works identically on web + Electron/Chromium):
 * - The <input> itself is ALWAYS isolated LTR (`dir="ltr"` +
 *   `unicode-bidi: isolate`). The ISO value (`yyyy-mm-dd`) is inherently LTR.
 * - Latin placeholders are prefixed with LEFT-TO-RIGHT MARK (`\u200E`).
 * - A global `.bidi-date-input` CSS companion (see apps/web globals.css)
 *   forces `::placeholder` + WebKit datetime segments to LTR/isolate.
 * - Native `type="date"` ignores `placeholder` in Chrome/Edge/Safari, so any
 *   human-readable format hint must be rendered OUTSIDE the input with
 *   `<bdi dir="ltr">` (see `DateFormatHint` below) — never as raw mixed text.
 *
 * Outer RTL layout is untouched: wrap <DateInput/> in your normal RTL
 * <label>/<div>; only the input box is LTR-isolated.
 */

export const LRM = '\u200E';

/** Canonical LTR format hint (Gregorian, ISO-style with slashes). */
export const DATE_FORMAT_HINT_LTR = 'yyyy/mm/dd' as const;

/** Localized Arabic format hint — pure RTL words, slashes resolve to RTL. */
export const DATE_FORMAT_HINT_AR = 'السنة/الشهر/اليوم' as const;

/**
 * Make a placeholder string safe to render inside an LTR-isolated input.
 * - Latin-containing placeholders get an LRM prefix so leading neutrals and
 *   the first strong-LTR run anchor correctly even if inherited dir is RTL.
 * - Pure Arabic placeholders are returned unchanged (RLM/LRM would corrupt
 *   their single RTL run).
 * - Idempotent: an existing leading LRM/RLM is not duplicated.
 */
export function bidiDatePlaceholder(format: string): string {
  if (!format) return format;
  const first = format.charAt(0);
  if (first === LRM || first === '\u200F') return format;
  // Latin letters or digits => needs LTR anchoring.
  if (/[A-Za-z0-9]/.test(format)) return `${LRM}${format}`;
  return format;
}

export type DateInputProps = Omit<
  React.InputHTMLAttributes<HTMLInputElement>,
  'type' | 'children'
> & {
  /**
   * Native date (default) keeps OS picker + a11y. `text` is a fallback for
   * manual `yyyy/mm/dd` entry where the native picker is unavailable.
   * @default 'date'
   */
  inputType?: 'date' | 'text';
  /**
   * Locale of the surrounding UI. Only affects the DEFAULT hint/placeholder:
   * 'ar' => `السنة/الشهر/اليوم`, 'en' => `yyyy/mm/dd`.
   * An explicit `placeholder` prop always wins.
   * @default 'ar'
   */
  locale?: 'ar' | 'en';
};

const DEFAULT_HINT: Record<NonNullable<DateInputProps['locale']>, string> = {
  ar: DATE_FORMAT_HINT_AR,
  en: DATE_FORMAT_HINT_LTR,
};

export const DateInput = React.forwardRef<HTMLInputElement, DateInputProps>(
  (
    {
      className,
      inputType = 'date',
      locale = 'ar',
      placeholder,
      style,
      dir = 'ltr',
      lang,
      ...props
    },
    ref,
  ) => {
    // Native date inputs ignore `placeholder` in Chromium/WebKit — still pass
    // a BiDi-safe value through for engines that DO show it (Firefox) and for
    // the `text` fallback mode where the placeholder is the format guide.
    const rawPlaceholder = placeholder ?? (inputType === 'text' ? DEFAULT_HINT[locale] : undefined);
    const safePlaceholder =
      rawPlaceholder !== undefined ? bidiDatePlaceholder(rawPlaceholder) : undefined;

    return (
      <input
        ref={ref}
        type={inputType}
        dir={dir}
        lang={lang}
        placeholder={safePlaceholder}
        data-bidi-date="true"
        // Inline isolation: survives even where globals.css is not loaded
        // (Electron setup wizard, print contexts). The class adds the
        // ::placeholder + ::-webkit-datetime-edit companions.
        style={{ direction: 'ltr', unicodeBidi: 'isolate', ...style }}
        className={cn(
          'bidi-date-input',
          'flex h-10 w-full rounded-[var(--radius-md)] border border-border bg-white px-3 text-sm',
          'placeholder:text-slate-400',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500',
          'disabled:cursor-not-allowed disabled:opacity-60',
          // Dates are numeric — tabular figures keep segments aligned.
          'numeric',
          className,
        )}
        {...props}
      />
    );
  },
);
DateInput.displayName = 'DateInput';

/**
 * Isolated format hint to render next to/under a date field.
 * Uses <bdi dir="ltr"> so `yyyy/mm/dd` never reorders inside RTL labels,
 * and renders Arabic hints as a single RTL run.
 *
 * @example
 * <Label>من تاريخ <DateFormatHint /></Label>
 */
export function DateFormatHint({
  locale = 'en',
  className,
}: {
  locale?: 'ar' | 'en';
  className?: string;
}) {
  const text = locale === 'ar' ? DATE_FORMAT_HINT_AR : `${LRM}${DATE_FORMAT_HINT_LTR}`;
  return (
    <bdi dir={locale === 'ar' ? 'rtl' : 'ltr'} className={cn('bidi-isolate', className)}>
      {text}
    </bdi>
  );
}
