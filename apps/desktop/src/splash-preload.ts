/**
 * Splash screen preload: contextBridge exposing the status event listener.
 */

import { contextBridge, ipcRenderer } from 'electron';

export interface SplashStatus {
  stage: string;
  message: string;
}

contextBridge.exposeInMainWorld('splashApi', {
  onStatus: (callback: (status: SplashStatus) => void) => {
    ipcRenderer.on('splash:status', (_event, data: SplashStatus) => callback(data));
  },
});
