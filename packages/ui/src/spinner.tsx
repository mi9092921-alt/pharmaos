import * as React from 'react';

import { cn } from './cn';

/** Loading indicator (CLAUDE.md UX rule: loading states on every async op). */
export function Spinner({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'inline-block size-5 animate-spin rounded-full border-2 border-primary-500 border-t-transparent',
        className,
      )}
      {...props}
    />
  );
}
