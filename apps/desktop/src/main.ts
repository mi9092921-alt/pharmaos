/**
 * Electron main process - the DEVICE ORCHESTRATOR (installer M4).
 *
 * Boot order: single-instance lock -> port preflight -> PostgreSQL
 * (device-init on first run) -> frozen API (/api/v1/health) -> migrate
 * (idempotent) -> bundled-Node web -> first-run wizard when
 * installation_state is incomplete -> main window. Shutdown reverses the
 * tree; the detached watchdog cleans up after a crash (decision 14).
 *
 * Security per CLAUDE.md: contextIsolation on, nodeIntegration off, external
 * navigation blocked. The setup wizard runs from a LOCAL file with a typed
 * IPC bridge (no remote content).
 */

import { app, BrowserWindow, ipcMain, dialog } from 'electron';
import path from 'node:path';

import { log, initLogger } from './runtime/logger';
import { Orchestrator } from './runtime/orchestrator';
import { childEnv, devicePaths, DevicePaths } from './runtime/paths';
import { runCapture } from './runtime/procs';

const p: DevicePaths = devicePaths();
let orchestrator: Orchestrator | null = null;
let setupWindow: BrowserWindow | null = null;
let shuttingDown = false;

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const win = BrowserWindow.getAllWindows()[0];
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  void app.whenReady().then(boot);
}

async function boot(): Promise<void> {
  initLogger(p.logsDir);
  log(`launcher ${app.isPackaged ? 'packaged' : 'dev'} starting; data dir ${p.dataDir}`);

  orchestrator = new Orchestrator({
    ...p,
    watchdogScript: path.join(p.desktopResources, 'watchdog.ps1'),
  });
  const result = await orchestrator.boot();

  if (!result.ok) {
    log(`boot failed at ${result.stage}: ${result.error}`);
    dialog.showErrorBox('PharmaOS - تعذر التشغيل', `${result.error}\n\nالسجلات: ${p.logsDir}`);
    app.quit();
    return;
  }

  if (result.setup && !result.setup.setup_complete) {
    log(`first run incomplete (step ${result.setup.last_completed_step}) - opening the wizard`);
    await runSetupWizard();
  }

  createMainWindow();
}

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

  const uiUrl = process.env.PHARMAOS_UI_URL ?? 'http://127.0.0.1:3000';
  // The device UI is local-only; block any external navigation.
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(uiUrl)) event.preventDefault();
  });

  void window.loadURL(uiUrl);
}

// ---------------------------------------------------------------------------
// First-run wizard (decisions 2/5/7): admin + branch via the CLI (no new API
// endpoints - P3-M8 posture), backup key shown once, tasks registered under
// the DAILY USER with the collected Windows password (never stored).
// ---------------------------------------------------------------------------

async function runSetupWizard(): Promise<void> {
  setupWindow = new BrowserWindow({
    width: 620,
    height: 720,
    resizable: false,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      preload: path.join(__dirname, 'setup-preload.js'),
    },
  });
  await setupWindow.loadFile(path.join(p.desktopResources, 'setup.html'));

  const window = setupWindow;
  const closed = new Promise<void>((resolve) => {
    window.on('closed', () => resolve());
  });

  ipcMain.handle(
    'setup:complete',
    async (
      _event,
      input: {
        username: string;
        fullName: string;
        password: string;
        branchName: string;
        winPassword: string;
      },
    ) => {
      try {
        // 1. the owner account (CLI; password via env - never argv)
        const admin = await runCapture(
          p.apiExe,
          ['bootstrap-admin', '--username', input.username, '--full-name', input.fullName],
          p.dataDir,
          undefined,
          { ...childEnv(p), PHARMAOS_ADMIN_PASSWORD: input.password },
        );
        if (admin.code !== 0) {
          throw new Error(
            admin.stderr || 'فشل إنشاء المالك (تحقق من قوة كلمة المرور وعدم تكرار الاسم).',
          );
        }
        // 2. the branch
        const branch = await runCapture(
          p.apiExe,
          ['bootstrap-branch', '--name', input.branchName],
          p.dataDir,
        );
        if (branch.code !== 0) throw new Error(branch.stderr || 'فشل إنشاء الفرع.');
        // 3. scheduled tasks under THE DAILY USER (decision 2) - "run whether
        //    logged on or not" so the 02:00 backup survives logout.
        await registerScheduledTasks(input.winPassword);
        // 4. the recovery key - shown ONCE (decision 5)
        const key = await runCapture(p.apiExe, ['backup', 'export-key'], p.dataDir);
        const backupKey = key.stdout.split('\n')[0]?.trim() ?? '';
        return { ok: true, backupKey };
      } catch (e) {
        log(`setup failed: ${e instanceof Error ? e.message : String(e)}`);
        return { ok: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
  );

  ipcMain.handle('setup:finish', async () => {
    try {
      const done = await runCapture(p.apiExe, ['setup-complete'], p.dataDir);
      if (done.code !== 0) throw new Error(done.stderr || 'فشل حفظ حالة الإعداد.');
      log('first-run setup complete');
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
  });

  await closed;
  ipcMain.removeHandler('setup:complete');
  ipcMain.removeHandler('setup:finish');
  setupWindow = null;
}

async function registerScheduledTasks(winPassword: string): Promise<void> {
  const user = process.env.USERNAME ?? '';
  if (!winPassword || !user) {
    log(
      'scheduled tasks: skipped (no Windows password provided) - the daily backup runs only while logged on',
    );
    return;
  }
  const tasks: Array<{ name: string; args: string; schedule: string[] }> = [
    {
      name: 'PharmaOS-Backup',
      args: 'backup create --no-cloud',
      schedule: ['/SC', 'DAILY', '/ST', '02:00'],
    },
    {
      name: 'PharmaOS-Compliance-Drain',
      args: 'compliance-drain',
      schedule: ['/SC', 'MINUTE', '/MO', '15'],
    },
    {
      name: 'PharmaOS-Expiry-Sweep',
      args: 'inventory expiry-sweep',
      schedule: ['/SC', 'DAILY', '/ST', '03:30'],
    },
    {
      name: 'PharmaOS-Alerts-Evaluate',
      args: 'alerts-evaluate',
      schedule: ['/SC', 'DAILY', '/ST', '03:45'],
    },
  ];
  for (const task of tasks) {
    const finalArgs = [
      '/Create',
      '/F',
      '/TN',
      task.name,
      '/TR',
      `"${p.apiExe}" ${task.args}`,
      ...task.schedule,
      '/RU',
      user,
      '/RP',
      winPassword,
    ];
    const result = await runCapture('schtasks.exe', finalArgs, p.dataDir);
    if (result.code !== 0) {
      throw new Error(`task ${task.name}: ${result.stderr || result.stdout}`);
    }
    log(`scheduled task registered: ${task.name} (user ${user}, run-whether-logged-on)`);
  }
}

// ---------------------------------------------------------------------------
// Shutdown: stop the tree; the watchdog covers the crash path.
// ---------------------------------------------------------------------------

app.on('before-quit', (event) => {
  if (shuttingDown) return;
  shuttingDown = true;
  log('before-quit: orchestrator shutdown');
  event.preventDefault();
  void orchestrator?.shutdown().finally(() => {
    app.exit(0);
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
