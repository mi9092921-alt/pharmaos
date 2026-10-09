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
let splashWindow: BrowserWindow | null = null;
let setupWindow: BrowserWindow | null = null;
let shuttingDown = false;
// True while the first-run wizard is on screen AND during the wizard -> main
// window handover (a moment with zero windows). While true, window-all-closed
// must NOT quit the app, or the main window never opens and the device looks
// stuck on "loading" (setup closes its last window before the main one exists).
let isSettingUp = false;

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const win = BrowserWindow.getAllWindows().find((w) => w !== splashWindow);
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  void app.whenReady().then(boot);
}

function createSplashWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 480,
    height: 320,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    center: true,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'splash-preload.js'),
    },
  });

  win.once('ready-to-show', () => {
    win.show();
  });

  void win.loadFile(path.join(p.desktopResources, 'splash.html'));
  return win;
}

async function boot(): Promise<void> {
  initLogger(p.logsDir);
  log(`launcher ${app.isPackaged ? 'packaged' : 'dev'} starting; data dir ${p.dataDir}`);

  // 1. Immediately display the splash window within < 200ms
  splashWindow = createSplashWindow();

  orchestrator = new Orchestrator({
    ...p,
    watchdogScript: path.join(p.desktopResources, 'watchdog.ps1'),
  });

  const updateProgress = (stage: string, message: string) => {
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.webContents.send('splash:status', { stage, message });
    }
  };

  const result = await orchestrator.boot(updateProgress);

  if (!result.ok) {
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.close();
      splashWindow = null;
    }
    log(`boot failed at ${result.stage}: ${result.error}`);
    dialog.showErrorBox('PharmaOS - تعذر التشغيل', `${result.error}\n\nالسجلات: ${p.logsDir}`);
    app.quit();
    return;
  }

  if (result.setup && !result.setup.setup_complete) {
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.close();
      splashWindow = null;
    }
    log(`first run incomplete (step ${result.setup.last_completed_step}) - opening the wizard`);
    isSettingUp = true;
    await runSetupWizard();
  }

  createMainWindow();
  isSettingUp = false;
}

function createMainWindow(): void {
  const window = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 1024,
    minHeight: 700,
    show: false,
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

  window.once('ready-to-show', () => {
    window.show();
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.close();
      splashWindow = null;
    }
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
        // 1. the owner account (CLI; password via env - never argv).
        //    Idempotent resume: an earlier attempt may have created the owner
        //    and then failed later (scheduled tasks / backup key). Treating
        //    "already exists" as success lets the wizard CONTINUE to the key
        //    and setup-complete instead of trapping the user in step 1 forever
        //    (every relaunch would reopen the wizard - the reported bug).
        const admin = await runCapture(
          p.apiExe,
          ['bootstrap-admin', '--username', input.username, '--full-name', input.fullName],
          p.dataDir,
          undefined,
          { ...childEnv(p), PHARMAOS_ADMIN_PASSWORD: input.password },
        );
        if (admin.code !== 0 && !/already exists/i.test(admin.stderr || '')) {
          throw new Error(
            admin.stderr || 'فشل إنشاء المالك (تحقق من قوة كلمة المرور وعدم تكرار الاسم).',
          );
        }
        if (admin.code !== 0) {
          log('setup: owner already exists - resuming first-run (keeping the existing account)');
        }
        // 2. the branch (already idempotent - "already exists" continues)
        const branch = await runCapture(
          p.apiExe,
          ['bootstrap-branch', '--name', input.branchName],
          p.dataDir,
        );
        if (branch.code !== 0 && !branch.stderr.includes('already exists')) {
          throw new Error(branch.stderr || 'فشل إنشاء الفرع.');
        }
        // 3. scheduled tasks under THE DAILY USER (decision 2) - "run whether
        //    logged on or not" so the 02:00 backup survives logout.
        //    NON-FATAL: a wrong Windows password must not block the backup key
        //    and setup-complete (the device would otherwise never finish
        //    first-run). The warning surfaces in the wizard; tasks can be
        //    re-registered later with the correct password.
        let tasksWarning: string | null = null;
        try {
          await registerScheduledTasks(input.winPassword);
        } catch (e) {
          tasksWarning = e instanceof Error ? e.message : String(e);
          log(`setup: scheduled tasks failed (non-fatal, retry later): ${tasksWarning}`);
        }
        // 4. the recovery key - shown ONCE (decision 5)
        const key = await runCapture(p.apiExe, ['backup', 'export-key'], p.dataDir);
        if (key.code !== 0) {
          throw new Error(key.stderr || 'فشل توليد مفتاح الاستعادة.');
        }
        const backupKey = key.stdout.split('\n')[0]?.trim() ?? '';
        if (!backupKey) {
          throw new Error('فشل توليد مفتاح الاستعادة (مفتاح فارغ).');
        }
        return { ok: true, backupKey, tasksWarning };
      } catch (e) {
        log(`setup failed: ${e instanceof Error ? e.message : String(e)}`);
        return { ok: false, error: e instanceof Error ? e.message : String(e) };
      }
    },
  );

  ipcMain.handle('setup:finish', async () => {
    try {
      const res = await fetch('http://127.0.0.1:8000/api/v1/system/setup-complete', {
        method: 'POST',
      });
      if (!res.ok) {
        const done = await runCapture(p.apiExe, ['setup-complete'], p.dataDir);
        if (done.code !== 0) throw new Error(done.stderr || 'فشل حفظ حالة الإعداد.');
      }
      log('first-run setup complete');
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
  });

  // Escape hatch for devices whose owner/branch already exist (e.g. an
  // interrupted first attempt): jump straight to login instead of forcing the
  // user through account creation again. Refuses when no account exists yet.
  ipcMain.handle('setup:skip', async () => {
    try {
      let users = 0;
      let branches = 0;
      try {
        const res = await fetch('http://127.0.0.1:8000/api/v1/system/setup-status');
        if (res.ok) {
          const json = (await res.json()) as { data?: { users?: number; branches?: number } };
          users = json.data?.users ?? 0;
          branches = json.data?.branches ?? 0;
        } else {
          throw new Error(`HTTP ${res.status}`);
        }
      } catch {
        const st = await runCapture(p.apiExe, ['setup-status'], p.dataDir);
        if (st.code !== 0) throw new Error(st.stderr || 'تعذر قراءة حالة الإعداد.');
        const parsed = JSON.parse(st.stdout) as { users?: number; branches?: number };
        users = parsed.users ?? 0;
        branches = parsed.branches ?? 0;
      }

      if (users <= 0) {
        return { ok: false, error: 'لا يوجد حساب على هذا الجهاز بعد — أكمل الإعداد أولاً.' };
      }

      if (branches <= 0) {
        log('setup:skip - bootstrapping default branch');
        await runCapture(p.apiExe, ['bootstrap-branch', '--name', 'الفرع الرئيسي'], p.dataDir);
      }

      const resDone = await fetch('http://127.0.0.1:8000/api/v1/system/setup-complete', {
        method: 'POST',
      });
      if (!resDone.ok) {
        const done = await runCapture(p.apiExe, ['setup-complete'], p.dataDir);
        if (done.code !== 0) throw new Error(done.stderr || 'فشل حفظ حالة الإعداد.');
      }
      log('first-run skipped (owner exists) - marked complete, opening login');
      setImmediate(() => setupWindow?.close());
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
  });

  await closed;
  ipcMain.removeHandler('setup:complete');
  ipcMain.removeHandler('setup:finish');
  ipcMain.removeHandler('setup:skip');
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
  // During first-run the wizard window closes BEFORE the main window exists
  // (a zero-window moment) - quitting here would kill the app before login
  // ever opens. The boot sequence owns the handover while isSettingUp.
  if (isSettingUp) return;
  if (process.platform !== 'darwin') app.quit();
});
