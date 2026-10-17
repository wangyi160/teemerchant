'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('teemerchant', Object.freeze({
  request: (method, path, body, identity) => ipcRenderer.invoke('merchant:request', method, path, body, identity),
  oauth: (action, body, identity) => ipcRenderer.invoke('merchant:oauth', action, body, identity),
  chooseFile: kind => ipcRenderer.invoke('merchant:choose-file', kind),
  copyText: text => ipcRenderer.invoke('merchant:copy-text', text),
  minimize: () => ipcRenderer.invoke('merchant:minimize'),
}));
