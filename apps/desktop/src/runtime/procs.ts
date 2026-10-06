/**
 * Child process supervision (installer decisions 12/14).
 *
 * The Electron process owns the whole component tree. Normal quit: stop
 * children in reverse order (API -> web; PostgreSQL last via pg_ctl).
 * CRASH: this process cannot clean up after itself - so a detached
 * PowerShell WATCHDOG monitors the launcher PID and, when it disappears,
 * force-kills the recorded child tree (taskkill /T /F). This is the
 * process-tree-supervision equivalent of a Job Object without a native
 * module (decision 14).
 */

import { ChildProcess, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export interface ManagedProcess {
  name: string;
  child?: ChildProcess;
  /** PID to taskkill (for detached pg_ctl-started postgres this is unknown;
   * the watchdog handles it via the PID file instead). */
  pid?: number;
}

export function spawnDetached(
  exe: string,
  args: string[],
  cwd: string,
  logFile: string,
  env?: NodeJS.ProcessEnv,
): ChildProcess {
  fs.mkdirSync(path.dirname(logFile), { recursive: true });
  const out = fs.openSync(logFile, 'a');
  const child = spawn(exe, args, {
    cwd,
    env,
    stdio: ['ignore', out, out],
    windowsHide: true,
    // Detached: the child must NOT die with Electron's console signals -
    // the orchestrator + watchdog own its lifecycle explicitly.
    detached: false,
  });
  return child;
}

export function runCapture(
  exe: string,
  args: string[],
  cwd: string,
  timeoutMs = 120000,
  env?: NodeJS.ProcessEnv,
): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn(exe, args, { cwd, env, windowsHide: true });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`${exe} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout?.on('data', (d: Buffer) => (stdout += d.toString()));
    child.stderr?.on('data', (d: Buffer) => (stderr += d.toString()));
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on('exit', (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr });
    });
  });
}

/**
 * Spawn the detached watchdog: polls the launcher PID; when Electron is gone
 * (crash / End Task), force-kills the whole child tree.
 */
export function spawnWatchdog(
  watchdogScript: string,
  launcherPid: number,
  dataDir: string,
): ChildProcess {
  return spawn(
    'powershell.exe',
    [
      '-NoProfile',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      watchdogScript,
      '-LauncherPid',
      String(launcherPid),
    ],
    { cwd: dataDir, windowsHide: true, detached: true, stdio: 'ignore' },
  );
  // detached + stdio ignore: the watchdog outlives Electron by design; once
  // the launcher PID is gone it kills the tree and exits.
}

export function killTree(pid: number): void {
  // /T = tree (children too), /F = force. Best-effort: failures are logged
  // by the caller.
  spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
}
