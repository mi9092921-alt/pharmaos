'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Fragment, useRef, useState } from 'react';

import { setI18nLocale } from '@/lib/i18n';
import { useLocaleStore } from '@/lib/locale-store';

export function Providers({
  initialLocale,
  children,
}: {
  initialLocale: 'ar' | 'en';
  children: React.ReactNode;
}) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: 1, refetchOnWindowFocus: false },
        },
      }),
  );

  // Re-sync the i18n module + store with the SERVER-resolved locale ONCE per
  // mount (server and client must agree — prevents hydration mismatch).
  // After that first sync the STORE is the source of truth: re-applying
  // initialLocale on every render would fight the user's client-side switch.
  const initializedRef = useRef(false);
  if (!initializedRef.current) {
    initializedRef.current = true;
    const storeLocale = useLocaleStore.getState().locale;
    if (storeLocale !== initialLocale) {
      useLocaleStore.setState({ locale: initialLocale });
    }
    setI18nLocale(initialLocale);
  }

  // key={locale} remounts the whole client tree on language switch — every
  // t() call re-evaluates without touching its call site. Local state (open
  // forms, the POS cart) is lost by design; the [ع|EN] toggle confirms first.
  const locale = useLocaleStore((s) => s.locale);
  return (
    <QueryClientProvider client={queryClient}>
      <Fragment key={locale}>{children}</Fragment>
    </QueryClientProvider>
  );
}
