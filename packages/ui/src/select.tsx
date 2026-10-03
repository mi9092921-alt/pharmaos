import * as React from 'react';

import { cn } from './cn';

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

/** Native select, styled to match Input. RTL-friendly (browser mirrors the arrow). */
export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, children, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        'h-10 rounded-[var(--radius-md)] border border-border bg-white px-3 text-sm',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500',
        'disabled:cursor-not-allowed disabled:opacity-60',
        className,
      )}
      {...props}
    >
      {children}
    </select>
  ),
);
Select.displayName = 'Select';
