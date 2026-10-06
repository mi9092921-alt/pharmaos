/** File logger: one device log the pharmacist can be talked through. */

import fs from 'node:fs';
import path from 'node:path';

let logFile: string | null = null;

export function initLogger(logsDir: string): void {
  fs.mkdirSync(logsDir, { recursive: true });
  logFile = path.join(logsDir, 'pharmaos.log');
  log(`--- PharmaOS launcher start (${new Date().toISOString()}) ---`);
}

export function log(message: string): void {
  const line = `${new Date().toISOString()} ${message}`;
  // eslint-disable-next-line no-console
  console.log(line);
  if (logFile) {
    try {
      fs.appendFileSync(logFile, line + '\n');
    } catch {
      /* disk full / permissions - console still has it */
    }
  }
}
