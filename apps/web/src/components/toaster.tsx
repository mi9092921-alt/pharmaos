'use client';

import { cn } from '@pharmaos/ui';

import { useToasts } from '@/lib/toast-store';

const TONE_CLASSES: Record<string, string> = {
  success: 'border-success/30 bg-green-50 text-success',
  error: 'border-danger/30 bg-red-50 text-danger',
  info: 'border-primary-500/30 bg-primary-50 text-primary-700',
};

/** Fixed toast stack (bottom-start under RTL). Rendered once in the app shell. */
export function Toaster() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);

  return (
    <div className="pointer-events-none fixed bottom-4 start-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => (
        <button
          key={t.id}
          onClick={() => dismiss(t.id)}
          className={cn(
            'pointer-events-auto min-w-64 rounded-[var(--radius-md)] border px-4 py-2 text-start text-sm shadow-md',
            TONE_CLASSES[t.tone],
          )}
        >
          {t.message}
        </button>
      ))}
    </div>
  );
}
