/**
 * Device paths (installer decision 3/4): the launcher FIXES production paths
 * (C:\ProgramData\PharmaOS) and mirrors the API's config contract. Dev mode
 * falls back to the repo layout so `pnpm --filter @pharmaos/desktop start`
 * still works on a developer machine.
 */

import { app } from 'electron';
import path from 'node:path';

export interface DevicePaths {
  /** Electron-side resources: watchdog.ps1 + setup.html (dev: apps/desktop/resources). */
  desktopResources: string;
  /** Install root for the frozen components (resources dir when packaged). */
  resourcesDir: string;
  /** API bundle: <resources>\api\pharmaos-api.exe */
  apiExe: string;
  /** Bundled PostgreSQL client/server binaries. */
  pgBin: string;
  /** Bundled web tree root (contains node.exe + apps/web/server.js). */
  webRoot: string;
  webServerJs: string;
  nodeExe: string;
  /** Device data root (pgdata/backups/logs/.env live under it). */
  dataDir: string;
  pgData: string;
  logsDir: string;
  backupsDir: string;
}

export function devicePaths(): DevicePaths {
  const isProd = app.isPackaged;
  const resourcesDir = isProd
    ? (process.resourcesPath ?? '')
    : path.join(process.cwd(), 'installer', 'dist');
  const desktopResources = isProd ? resourcesDir : path.join(__dirname, '..', 'resources');
  const dataDir =
    process.env.PHARMAOS_DATA_DIR ??
    path.join(process.env.PROGRAMDATA ?? 'C:\\ProgramData', 'PharmaOS');
  return {
    resourcesDir,
    desktopResources,
    apiExe: path.join(resourcesDir, 'api', 'pharmaos-api.exe'),
    pgBin: path.join(resourcesDir, 'pg', 'bin'),
    webRoot: path.join(resourcesDir, 'web'),
    webServerJs: path.join(resourcesDir, 'web', 'apps', 'web', 'server.js'),
    nodeExe: path.join(resourcesDir, 'web', 'node.exe'),
    dataDir,
    pgData: path.join(dataDir, 'pgdata'),
    logsDir: path.join(dataDir, 'logs'),
    backupsDir: path.join(dataDir, 'backups'),
  };
}

/** Non-secret env passed to EVERY child (the API reads .env + keystore). */
export function childEnv(p: DevicePaths): NodeJS.ProcessEnv {
  return {
    ...process.env,
    PHARMAOS_ENV: 'production',
    PG_BIN_DIR: p.pgBin,
    // production ignores user DATABASE_URL/PHARMAOS_* overrides inside the
    // API (decision 3) - the launcher still pins them for the child trees.
    PHARMAOS_DATA_DIR: p.dataDir,
  };
}
