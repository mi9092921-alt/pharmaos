'use client';

import { Icon, type IconName } from '@pharmaos/ui';
import { cn } from '@pharmaos/ui';

import { useToasts } from '@/lib/toast-store';

const TONE_CLASSES: Record<string, string> = {
  success: 'border-success/30 bg-green-50 text-success',
  error: 'border-danger/30 bg-red-50 text-danger',
  info: 'border-primary-500/30 bg-primary-50 text-primary-700',
};

const TONE_ICONS: Record<string, IconName> = {
  success: 'tone_success',
  error: 'tone_error',
  info: 'tone_info',
};

/** Fixed toast stack (bottom-start under RTL). Rendered once in the app shell. */
export function Toaster() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);

  return (
    <div className="pointer-events-none fixed bottom-4 start-4 z-50 flex flex-col gap-2">
      <div role="status" aria-live="polite" className="contents">
        {toasts.map((t) => {
          const toneIcon = TONE_ICONS[t.tone];
          return (
            <button
              key={t.id}
              onClick={() => dismiss(t.id)}
              className={cn(
                'pointer-events-auto flex min-w-64 items-start gap-2 rounded-[var(--radius-md)] border px-4 py-2 text-start text-sm shadow-md motion-reduce:transition-none',
                TONE_CLASSES[t.tone],
              )}
            >
              {toneIcon && <Icon name={toneIcon} size={15} className="mt-0.5" />}
              <span className="flex-1">{t.message}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
