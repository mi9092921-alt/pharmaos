'use client';

import * as React from 'react';

import { cn } from './cn';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  className?: string;
}

/**
 * Minimal accessible modal (CLAUDE.md UX rule: never use the browser's default
 * alert/confirm/prompt). Escape closes; clicking the backdrop closes; content
 * click is contained. Focus moves to the dialog on open.
 */
export function Modal({ open, onClose, title, children, className }: ModalProps) {
  const ref = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    ref.current?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onMouseDown={onClose}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onMouseDown={(e) => e.stopPropagation()}
        className={cn(
          'w-full max-w-md rounded-[var(--radius-lg)] border border-border bg-white p-6 shadow-xl outline-none',
          className,
        )}
      >
        {title && <h2 className="mb-4 text-lg font-bold text-slate-900">{title}</h2>}
        {children}
      </div>
    </div>
  );
}
