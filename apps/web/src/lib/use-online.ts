'use client';

import { useEffect, useState } from 'react';

/**
 * Online/offline indicator (CLAUDE.md UX rule: a clear offline badge + a
 * persistent status indicator). PharmaOS is offline-first — losing connectivity
 * must never block work; this only drives the status badge.
 */
export function useOnline(): boolean {
  // Assume online during SSR/first paint; correct on mount.
  const [online, setOnline] = useState(true);

  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    update();
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    return () => {
      window.removeEventListener('online', update);
      window.removeEventListener('offline', update);
    };
  }, []);

  return online;
}
