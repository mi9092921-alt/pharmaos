'use client';

import * as React from 'react';

import { cn } from './cn';

export interface TabItem<T extends string> {
  value: T;
  label: React.ReactNode;
  /** Hidden entirely (permission-gated tabs) — the bar renders nothing for it. */
  hidden?: boolean;
}

export interface TabsProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  items: TabItem<T>[];
  className?: string;
}

/**
 * The app's single tab bar (RC-A wave و): replaces the hand-rolled
 * border-b/px-4/py-2 pattern previously duplicated across 6+ pages, with
 * proper `role=tablist`/`aria-selected` semantics baked in.
 */
export function Tabs<T extends string>({ value, onChange, items, className }: TabsProps<T>) {
  const visible = items.filter((it) => !it.hidden);
  if (visible.length === 0) return null;
  return (
    <div role="tablist" className={cn('flex gap-1 border-b border-border', className)}>
      {visible.map((it) => (
        <button
          key={it.value}
          type="button"
          role="tab"
          aria-selected={value === it.value}
          onClick={() => onChange(it.value)}
          className={cn(
            // py-2.5 keeps the tab bar ≈40px — navigation control, not a form field.
            'border-b-2 px-4 py-2.5 text-sm font-semibold transition-colors',
            value === it.value
              ? 'border-primary-600 text-primary-700'
              : 'border-transparent text-slate-500 hover:text-slate-700',
          )}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}
