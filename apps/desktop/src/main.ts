/**
 * Electron main process — Phase 0 scaffold.
 *
 * Loads the PharmaOS UI (Next.js on localhost). Security hardening per
 * CLAUDE.md: context isolation on, node integration off, external navigation
 * blocked. ESC/POS printing, cash-drawer pulse, and safeStorage-backed key
 * flows are Phase 1 scope (walking-skeleton / POS milestones).
 */

import { app, BrowserWindow } from 'electron';
import path from 'node:path';

const UI_URL = process.env.PHARMAOS_UI_URL ?? 'http://127.0.0.1:3000';

function createMainWindow(): void {
  const window = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 1024,
    minHeight: 700,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  // The device UI is local-only; block any external navigation.
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(UI_URL)) event.preventDefault();
  });

  void window.loadURL(UI_URL);
}

app.whenReady().then(() => {
  createMainWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
