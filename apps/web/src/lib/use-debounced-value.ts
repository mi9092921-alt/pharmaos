'use client';

import { useEffect, useState } from 'react';

/**
 * Debounce any fast-changing value (search inputs) — wave UX-د: the supplier
 * and customer pickers previously fired a request per keystroke.
 */
export function useDebouncedValue<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);

  return debounced;
}
