'use client';

import * as React from 'react';

import { cn } from './cn';

/**
 * Overlay registry — modal nesting support (RC-A phase 3): Escape closes ONLY
 * the topmost overlay, and the backdrop click belongs to it too. Without this,
 * a ConfirmDialog stacked over an editor Modal closes BOTH on one Escape.
 */
const overlayStack: number[] = [];
let overlaySeq = 0;

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
  const idRef = React.useRef<number>(0);

  React.useEffect(() => {
    if (!open) return;
    const id = ++overlaySeq;
    idRef.current = id;
    overlayStack.push(id);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && overlayStack[overlayStack.length - 1] === id) onClose();
    };
    document.addEventListener('keydown', onKey);
    ref.current?.focus();
    return () => {
      const at = overlayStack.indexOf(id);
      if (at >= 0) overlayStack.splice(at, 1);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onMouseDown={() => {
        // Backdrop close belongs to THIS overlay only when it is the topmost
        // (the dialog content stopPropagation already shields inner clicks).
        if (overlayStack[overlayStack.length - 1] === idRef.current) onClose();
      }}
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
