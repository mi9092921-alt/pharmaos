/**
 * Port pre-flight (installer M4): a busy port must produce a NAMED error
 * before any component starts - never a silent hang.
 */

import net from 'node:net';

export async function isPortFree(port: number, host = '127.0.0.1'): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => {
      server.close(() => resolve(true));
    });
    server.listen(port, host);
  });
}

export interface PortCheck {
  port: number;
  label: string;
  free: boolean;
}

export async function preflightPorts(p: {
  pgPort: number;
  apiPort: number;
  webPort: number;
}): Promise<PortCheck[]> {
  return [
    { port: p.pgPort, label: 'PostgreSQL (5433)', free: await isPortFree(p.pgPort) },
    { port: p.apiPort, label: 'API (8000)', free: await isPortFree(p.apiPort) },
    { port: p.webPort, label: 'الواجهة (3000)', free: await isPortFree(p.webPort) },
  ];
}
