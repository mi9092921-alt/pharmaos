import { cva, type VariantProps } from 'class-variance-authority';
import * as React from 'react';

import { cn } from './cn';

const badgeVariants = cva(
  'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium',
  {
    variants: {
      tone: {
        neutral: 'bg-slate-100 text-slate-700',
        success: 'bg-green-100 text-success',
        warning: 'bg-amber-100 text-warning',
        danger: 'bg-red-100 text-danger',
        primary: 'bg-primary-50 text-primary-700',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}
