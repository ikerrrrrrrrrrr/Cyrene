const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('cyrene', {
  platform: process.platform,
  version: process.env.npm_package_version || '0.0.0',
});
