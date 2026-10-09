/**
 * Setup-wizard preload: the ONLY bridge the local setup page gets - a typed
 * completeSetup/finishSetup pair over contextBridge (contextIsolation on).
 */

import { contextBridge, ipcRenderer } from 'electron';

export interface SetupInput {
  username: string;
  fullName: string;
  password: string;
  branchName: string;
  winPassword: string;
}

contextBridge.exposeInMainWorld('pharmaosSetup', {
  completeSetup: (input: SetupInput) => ipcRenderer.invoke('setup:complete', input),
  finishSetup: () => ipcRenderer.invoke('setup:finish'),
  skipSetup: () => ipcRenderer.invoke('setup:skip'),
});
