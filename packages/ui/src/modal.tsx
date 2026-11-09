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
/** Body scroll-lock refcount — nested modals release only when ALL close. */
let scrollLocks = 0;

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export type ModalSize = 'sm' | 'md' | 'lg' | 'xl';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  className?: string;
  /** Width preset (className still wins via tailwind-merge when both set). */
  size?: ModalSize;
}

/**
 * Minimal accessible modal (CLAUDE.md UX rule: never use the browser's default
 * alert/confirm/prompt). Escape closes; clicking the backdrop closes; content
 * click is contained. Focus moves to the dialog on open.
 */
const SIZE_CLASS: Record<ModalSize, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-2xl',
};

export function Modal({ open, onClose, title, children, className, size = 'md' }: ModalProps) {
  const ref = React.useRef<HTMLDivElement>(null);
  const idRef = React.useRef<number>(0);

  React.useEffect(() => {
    if (!open) return;
    const id = ++overlaySeq;
    idRef.current = id;
    overlayStack.push(id);

    scrollLocks += 1;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // Autofocus: first field when present (forms), else the dialog itself.
    const firstField = ref.current?.querySelector<HTMLElement>(FOCUSABLE);
    (firstField ?? ref.current)?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (overlayStack[overlayStack.length - 1] !== id) return;
      if (e.key === 'Escape') {
        onClose();
        return;
      }
      if (e.key === 'Tab' && ref.current) {
        // Focus trap: wrap Tab / Shift+Tab inside this dialog.
        const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE));
        if (items.length === 0) {
          e.preventDefault();
          return;
        }
        const first = items.at(0);
        const last = items.at(-1);
        if (!first || !last) {
          e.preventDefault();
          return;
        }
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey && (active === first || !ref.current.contains(active))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && active === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);

    return () => {
      const at = overlayStack.indexOf(id);
      if (at >= 0) overlayStack.splice(at, 1);
      document.removeEventListener('keydown', onKey);
      scrollLocks -= 1;
      if (scrollLocks <= 0) document.body.style.overflow = prevOverflow;
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
          'w-full rounded-[var(--radius-lg)] border border-border bg-white p-6 shadow-xl outline-none',
          SIZE_CLASS[size],
          className,
        )}
      >
        {title && <h2 className="mb-4 text-lg font-bold text-slate-900">{title}</h2>}
        {children}
      </div>
    </div>
  );
}
