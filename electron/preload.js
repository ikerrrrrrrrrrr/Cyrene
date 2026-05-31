const { contextBridge } = require('electron');
const { ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cyrene', {
  platform: process.platform,
  version: process.env.npm_package_version || '0.0.0',
  getDesktopSettings: () => ipcRenderer.invoke('desktop-settings:get'),
  updateDesktopSettings: (updates) => ipcRenderer.invoke('desktop-settings:update', updates),
  showNotification: ({ title, body }) => ipcRenderer.invoke('notification:show', { title, body }),
});
