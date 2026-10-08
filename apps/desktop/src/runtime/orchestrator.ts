/**
 * The orchestrator (installer M4): starts/stops the device stack in order -
 *   preflight ports -> PostgreSQL (device-init on first run) -> API
 *   (wait /api/v1/health) -> migrate (idempotent) -> web (wait /) ->
 *   setup state -> main window; shutdown reverses it. The watchdog cleans up
 *   after a crash (decision 14).
 */

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';

import { log } from './logger';
import { childEnv, DevicePaths } from './paths';
import { ManagedProcess, killTree, runCapture, spawnDetached, spawnWatchdog } from './procs';
import { preflightPorts } from './ports';

const PG_PORT = 5433;
const API_PORT = 8000;
const WEB_PORT = 3000;

export interface OrchestratorPaths extends DevicePaths {
  watchdogScript: string;
}

export type BootStage = 'preflight' | 'postgres' | 'api' | 'migrate' | 'web' | 'setup' | 'ready';

export interface BootResult {
  ok: boolean;
  stage: BootStage;
  error?: string;
  /** The CLI's setup-status JSON when the stack is up (first-run wizard). */
  setup?: {
    users: number;
    branches: number;
    setup_complete: boolean;
    last_completed_step: string;
  };
}

async function waitHttp(url: string, timeoutMs: number, what: string): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ok = await new Promise<boolean>((resolve) => {
      const req = http.get(url, { timeout: 2000 }, (res) => {
        res.resume();
        resolve((res.statusCode ?? 500) < 500);
      });
      req.on('error', () => resolve(false));
      req.on('timeout', () => {
        req.destroy();
        resolve(false);
      });
    });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`${what} did not become healthy at ${url} within ${timeoutMs / 1000}s`);
}

export class Orchestrator {
  private procs: ManagedProcess[] = [];
  private stopping = false;

  constructor(private p: OrchestratorPaths) {}

  async boot(onProgress?: (stage: string, message: string) => void): Promise<BootResult> {
    const env = childEnv(this.p);
    try {
      onProgress?.('preflight', 'جاري فحص المنافذ...');
      log('boot: preflight ports');
      const checks = await preflightPorts({
        pgPort: PG_PORT,
        apiPort: API_PORT,
        webPort: WEB_PORT,
      });
      const busy = checks.filter((c) => !c.free);

      // Only invoke PowerShell cleanup if previous session left orphaned processes
      if (busy.length > 0) {
        log(
          `boot: busy ports detected (${busy.map((b) => b.label).join(', ')}), cleaning leftovers`,
        );
        onProgress?.('cleanup', 'جاري إخلاء المنافذ السابقة...');
        await runCapture(
          'powershell.exe',
          ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', this.p.watchdogScript, '-Cleanup'],
          this.p.dataDir,
          15000,
        );
        await new Promise((r) => setTimeout(r, 400));

        const postCleanup = await preflightPorts({
          pgPort: PG_PORT,
          apiPort: API_PORT,
          webPort: WEB_PORT,
        });
        const stillBusy = postCleanup.filter((c) => !c.free);
        if (stillBusy.length > 0) {
          const detail = stillBusy.map((b) => `${b.label} is in use`).join('; ');
          return {
            ok: false,
            stage: 'preflight',
            error: `Ports busy - ${detail}. Close the program using them and reopen PharmaOS.`,
          };
        }
      } else {
        log('boot: all ports free, skipping cleanup');
      }

      // ---- PostgreSQL: device-init on first run, then pg_ctl start --------
      onProgress?.('postgres', 'جاري تشغيل قاعدة البيانات...');
      log('boot: postgres');
      fs.mkdirSync(this.p.logsDir, { recursive: true });
      if (!fs.existsSync(path.join(this.p.pgData, 'PG_VERSION'))) {
        log('boot: device-init (first run - initdb + keystore)');
        onProgress?.('postgres', 'جاري تهيئة قاعدة البيانات لأول مرة...');
        const init = await runCapture(this.p.apiExe, ['device-init'], this.p.dataDir, 300000);
        if (init.code !== 0) {
          return {
            ok: false,
            stage: 'postgres',
            error: `device-init failed: ${init.stderr || init.stdout}`,
          };
        }
      }
      const pgStart = await runCapture(
        path.join(this.p.pgBin, 'pg_ctl.exe'),
        ['-D', this.p.pgData, 'start', '-l', path.join(this.p.logsDir, 'pg.log')],
        this.p.dataDir,
      );
      if (
        pgStart.code !== 0 &&
        !/already running|not a database/i.test(pgStart.stderr + pgStart.stdout)
      ) {
        return {
          ok: false,
          stage: 'postgres',
          error: `pg_ctl start failed: ${pgStart.stderr || pgStart.stdout}`,
        };
      }
      // pg_isready (bundled binary) - the DB must answer before the services boot.
      const ready = await runCapture(
        path.join(this.p.pgBin, 'pg_isready.exe'),
        ['-h', '127.0.0.1', '-p', String(PG_PORT)],
        this.p.dataDir,
        60000,
      );
      if (ready.code !== 0) {
        return { ok: false, stage: 'postgres', error: `PostgreSQL not ready: ${ready.stdout}` };
      }

      // ---- Parallel Boot: spawn API and Web concurrently -------------------
      onProgress?.('services', 'جاري تشغيل الخدمات وواجهة المستخدم...');
      log('boot: spawning api & web in parallel');

      const api = spawnDetached(
        this.p.apiExe,
        [],
        this.p.dataDir,
        path.join(this.p.logsDir, 'api.log'),
      );
      this.procs.push({ name: 'api', child: api, pid: api.pid });

      const web = spawnDetached(
        this.p.nodeExe,
        [this.p.webServerJs],
        this.p.webRoot,
        path.join(this.p.logsDir, 'web.log'),
        {
          ...env,
          PORT: String(WEB_PORT),
          HOSTNAME: '127.0.0.1',
          PHARMAOS_API_INTERNAL_URL: `http://127.0.0.1:${API_PORT}`,
        },
      );
      this.procs.unshift({ name: 'web', child: web, pid: web.pid });

      // Wait for both API health and Web UI readiness in parallel
      await Promise.all([
        waitHttp(`http://127.0.0.1:${API_PORT}/api/v1/health`, 90000, 'API'),
        waitHttp(`http://127.0.0.1:${WEB_PORT}/`, 90000, 'Web UI'),
      ]);

      // ---- watchdog: cleans the tree if THIS process dies (decision 14) ----
      log('boot: watchdog');
      spawnWatchdog(this.p.watchdogScript, process.pid, this.p.dataDir);

      // ---- first-run state: query over fast HTTP (falls back to CLI) -------
      onProgress?.('setup', 'جاري فحص حالة النظام...');
      log('boot: setup-status');
      let setup: BootResult['setup'];
      try {
        const res = await fetch(`http://127.0.0.1:${API_PORT}/api/v1/system/setup-status`);
        if (res.ok) {
          const json = (await res.json()) as { success?: boolean; data?: BootResult['setup'] };
          setup = json.data;
        } else {
          throw new Error(`HTTP ${res.status}`);
        }
      } catch (e) {
        log(`boot: setup-status HTTP query failed, falling back to CLI: ${e}`);
        const status = await runCapture(this.p.apiExe, ['setup-status'], this.p.dataDir);
        try {
          setup = JSON.parse(status.stdout) as NonNullable<BootResult['setup']>;
        } catch {
          setup = undefined;
        }
      }

      onProgress?.('ready', 'تم تشغيل النظام بنجاح');
      return { ok: true, stage: 'ready', setup };
    } catch (e) {
      const err = e instanceof Error ? e.message : String(e);
      log(`boot FAILED: ${err}`);
      await this.shutdown();
      return { ok: false, stage: 'preflight', error: err };
    }
  }

  /** Reverse-order shutdown; safe to call twice. */
  async shutdown(): Promise<void> {
    if (this.stopping) return;
    this.stopping = true;
    log('shutdown: stopping children (web -> api)');
    for (const proc of this.procs) {
      if (proc.pid) killTree(proc.pid);
    }
    this.procs = [];
    try {
      await runCapture(
        path.join(this.p.pgBin, 'pg_ctl.exe'),
        ['-D', this.p.pgData, 'stop', '-m', 'fast'],
        this.p.dataDir,
        60000,
      );
      log('shutdown: postgres stopped');
    } catch (e) {
      log(`shutdown: pg_ctl stop issue: ${e instanceof Error ? e.message : String(e)}`);
    }
    this.stopping = false;
  }
}
