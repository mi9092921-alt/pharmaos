/** File logger: one device log the pharmacist can be talked through.
 *
 * Rotation (release-prep phase 7): pharmaos.log grows on every launcher line —
 * without a cap it runs for months on an always-on device. One-generation
 * rollover at 5 MB (current -> .old): keeps yesterday's diagnostics while the
 * steady-state footprint stays bounded. */

import fs from 'node:fs';
import path from 'node:path';

const MAX_LOG_BYTES = 5 * 1024 * 1024;

let logFile: string | null = null;

export function initLogger(logsDir: string): void {
  fs.mkdirSync(logsDir, { recursive: true });
  logFile = path.join(logsDir, 'pharmaos.log');
  rotateIfNeeded(logFile);
  log(`--- PharmaOS launcher start (${new Date().toISOString()}) ---`);
}

function rotateIfNeeded(file: string): void {
  try {
    if (fs.existsSync(file) && fs.statSync(file).size >= MAX_LOG_BYTES) {
      fs.rmSync(`${file}.old`, { force: true });
      fs.renameSync(file, `${file}.old`);
    }
  } catch {
    /* rotation is best-effort - never block startup on it */
  }
}

export function log(message: string): void {
  const line = `${new Date().toISOString()} ${message}`;
  // eslint-disable-next-line no-console
  console.log(line);
  if (logFile) {
    try {
      rotateIfNeeded(logFile);
      fs.appendFileSync(logFile, line + '\n');
    } catch {
      /* disk full / permissions - console still has it */
    }
  }
}
