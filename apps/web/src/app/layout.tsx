import type { Metadata } from 'next';

// Self-hosted fonts (offline-first — no Google Fonts dependency at build OR
// runtime, per CLAUDE.md). Cairo = Arabic UI (ships the Arabic subset),
// Inter = numbers. Family names: 'Cairo Variable' / 'Inter Variable'.
import '@fontsource-variable/cairo/index.css';
import '@fontsource-variable/inter/index.css';

import { Providers } from './providers';
import './globals.css';

export const metadata: Metadata = {
  title: 'PharmaOS — نظام إدارة الصيدلية',
  description: 'نظام إدارة صيدلية يعمل offline مع مزامنة سحابية',
};

/**
 * Root layout — FULL RTL (CLAUDE.md UX rules: no mixed RTL/LTR in a page).
 * Default locale is Arabic; the i18n config also supports 'en'.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ar" dir="rtl">
      <body className="min-h-screen bg-surface antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
