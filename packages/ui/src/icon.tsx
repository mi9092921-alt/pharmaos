'use client';

import {
  Bell,
  BookOpen,
  CheckCircle2,
  CircleAlert,
  Info,
  CalendarCheck,
  ClipboardList,
  CreditCard,
  FileText,
  LayoutDashboard,
  LogOut,
  type LucideIcon,
  Package,
  Receipt,
  RotateCcw,
  Settings,
  ShieldCheck,
  ShoppingBag,
  ShoppingCart,
  TriangleAlert,
  UserRound,
  Users,
} from 'lucide-react';
import * as React from 'react';

import { cn } from './cn';

/**
 * Thin wrapper over lucide-react so pages never import the library directly —
 * one place to pin sizing/stroke behavior and one swap point if the icon
 * stack ever changes. Icons are stroke-based glyphs (lucide is MIT); arrows
 * that carry direction meaning must be flipped by callers via
 * `rtl:-scale-x-100` — the glyphs themselves stay direction-neutral here.
 */

export const ICONS = {
  dashboard: LayoutDashboard,
  pos: ShoppingCart,
  catalog: BookOpen,
  inventory: Package,
  stocktake: ClipboardList,
  shortages: TriangleAlert,
  purchases: ShoppingBag,
  customers: Users,
  returns: RotateCcw,
  prescriptions: FileText,
  finance: CreditCard,
  compliance: ShieldCheck,
  cashier: Receipt,
  alerts: TriangleAlert,
  notifications: Bell,
  reports: FileText,
  users: UserRound,
  settings: Settings,
  about: ShieldCheck,
  logout: LogOut,
  bell: Bell,
  tone_success: CheckCircle2,
  tone_error: CircleAlert,
  tone_info: Info,
  calendar: CalendarCheck,
} as const;

export type IconName = keyof typeof ICONS;

export function Icon({
  name,
  className,
  size = 16,
  strokeWidth = 2,
}: {
  name: IconName;
  className?: string;
  size?: number;
  strokeWidth?: number;
}) {
  const Glyph = ICONS[name] as LucideIcon;
  return (
    <Glyph
      size={size}
      strokeWidth={strokeWidth}
      className={cn('shrink-0', className)}
      aria-hidden
    />
  );
}
