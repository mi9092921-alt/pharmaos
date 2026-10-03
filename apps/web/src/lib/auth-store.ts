/**
 * Session state (Zustand — CLAUDE.md state stack).
 * Session data lives in httpOnly cookies server-side; this store holds only the
 * non-sensitive user profile for UI decisions. Nothing sensitive touches
 * localStorage (forbidden action #10).
 */

import { hasPermission } from '@pharmaos/shared';
import { create } from 'zustand';

import type { SessionUser } from './api';

interface AuthState {
  user: SessionUser | null;
  setUser: (user: SessionUser | null) => void;
  hasPermission: (permission: string) => boolean;
}

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  setUser: (user) => set({ user }),
  hasPermission: (permission) => hasPermission(get().user?.role, permission),
}));
