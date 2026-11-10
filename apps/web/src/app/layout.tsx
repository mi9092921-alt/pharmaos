import type { Metadata } from 'next';
import { cookies } from 'next/headers';

// Self-hosted fonts (offline-first — no Google Fonts dependency at build OR
// runtime, per CLAUDE.md). Cairo = Arabic UI (ships the Arabic subset),
// Inter = numbers. Family names: 'Cairo Variable' / 'Inter Variable'.
import '@fontsource-variable/cairo/index.css';
import '@fontsource-variable/inter/index.css';

import { Providers } from './providers';
import './globals.css';

async function requestLocale(): Promise<'ar' | 'en'> {
  return (await cookies()).get('pharmaos_locale')?.value === 'en' ? 'en' : 'ar';
}

export async function generateMetadata(): Promise<Metadata> {
  const locale = await requestLocale();
  return locale === 'en'
    ? {
        title: 'PharmaOS — Pharmacy Management System',
        description: 'Offline-first pharmacy management with cloud sync',
        icons: {
          icon: '/brand/pharmaos-icon.svg',
          shortcut: '/brand/pharmaos-icon.svg',
          apple: '/brand/pharmaos-icon.svg',
        },
      }
    : {
        title: 'PharmaOS — نظام إدارة الصيدلية',
        description: 'نظام إدارة صيدلية يعمل offline مع مزامنة سحابية',
        icons: {
          icon: '/brand/pharmaos-icon.svg',
          shortcut: '/brand/pharmaos-icon.svg',
          apple: '/brand/pharmaos-icon.svg',
        },
      };
}

/**
 * Root layout — FULL RTL (CLAUDE.md UX rules: no mixed RTL/LTR in a page).
 * Default locale is Arabic; the i18n config also supports 'en'.
 */
export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const locale = await requestLocale();
  return (
    <html lang={locale} dir={locale === 'ar' ? 'rtl' : 'ltr'}>
      <body className="min-h-screen bg-surface antialiased">
        <Providers initialLocale={locale}>{children}</Providers>
      </body>
    </html>
  );
}
