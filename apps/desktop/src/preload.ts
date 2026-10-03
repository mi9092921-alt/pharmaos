/**
 * Preload — the only bridge between renderer and main (context-isolated).
 * Phase 0 exposes app metadata only; Phase 1 adds the printing/drawer IPC
 * surface (ESC/POS) behind explicit, narrow channels.
 */

import { contextBridge } from 'electron';

contextBridge.exposeInMainWorld('pharmaos', {
  platform: process.platform,
  appVersion: process.env.npm_package_version ?? '1.1.0',
});
