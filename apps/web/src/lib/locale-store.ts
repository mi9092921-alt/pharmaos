'use client';

import { create } from 'zustand';

import { setI18nLocale, type Locale } from './i18n';

/**
 * Locale runtime (RC wave هـ): the cookie `pharmaos_locale` is the persisted
 * source (server layout reads it for lang/dir), this store mirrors it for
 * reactive UI (the [ع|EN] toggle) and drives i18n module state + the
 * document attributes on every switch. Switching REMOUNTS the app tree
 * (Providers wraps children in <Fragment key={locale}>) — local state is lost
 * by design and the toggle confirms before switching.
 */
const COOKIE = 'pharmaos_locale';

function readLocaleCookie(): Locale | null {
  if (typeof document === 'undefined') return null;
  const m = document.cookie.match(/(?:^|;\s*)pharmaos_locale=(ar|en)/);
  return m ? (m[1] as Locale) : null;
}

function writeLocaleCookie(locale: Locale): void {
  document.cookie = `${COOKIE}=${locale}; path=/; max-age=31536000; samesite=strict`;
}

function applyDocumentAttrs(locale: Locale): void {
  document.documentElement.lang = locale;
  document.documentElement.dir = locale === 'ar' ? 'rtl' : 'ltr';
}

interface LocaleState {
  locale: Locale;
  /** Hydrate from the server layout value (idempotent, pre-render). */
  init: (locale: Locale) => void;
  /** User-driven switch: persists the cookie + applies everything. */
  setLocale: (locale: Locale) => void;
}

export const useLocaleStore = create<LocaleState>((set) => ({
  locale: 'ar',
  init: (locale) => {
    setI18nLocale(locale);
    set({ locale });
  },
  setLocale: (locale) => {
    setI18nLocale(locale);
    writeLocaleCookie(locale);
    applyDocumentAttrs(locale);
    set({ locale });
  },
}));

/** Client-side boot read (login page / topbar before Providers init). */
export function localeFromCookie(): Locale {
  return readLocaleCookie() ?? 'ar';
}
