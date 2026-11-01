'use client';

import * as React from 'react';

import { Button } from './button';
import { Modal } from './modal';

export interface ConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: React.ReactNode;
  confirmLabel: string;
  cancelLabel: string;
  /** Busy state — both buttons disable while the confirmed mutation runs. */
  pending?: boolean;
  /** Default true: irreversible actions confirm with the danger button. */
  danger?: boolean;
}

/**
 * App-standard confirmation for irreversible actions (CLAUDE.md UX rule:
 * never the browser's alert/confirm/prompt). A thin, label-injected wrapper
 * over Modal — labels come from callers so this package stays i18n-free.
 * (Phase 0b: gains an overlay registry so nested dialogs scope Escape to the
 * topmost layer.)
 */
export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel,
  cancelLabel,
  pending,
  danger = true,
}: ConfirmDialogProps) {
  return (
    <Modal open={open} onClose={onClose} title={title} className="max-w-md">
      <div className="space-y-4">
        <div className="text-sm leading-relaxed text-slate-700">{message}</div>
        <div className="flex justify-end gap-2 border-t border-border pt-4">
          <Button type="button" variant="outline" onClick={onClose} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            type="button"
            variant={danger ? 'danger' : 'primary'}
            onClick={onConfirm}
            disabled={pending}
          >
            {confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
