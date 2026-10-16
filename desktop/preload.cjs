'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('teemerchant', Object.freeze({
  request: (method, path, body) => ipcRenderer.invoke('merchant:request', method, path, body),
  chooseFile: kind => ipcRenderer.invoke('merchant:choose-file', kind),
  copyText: text => ipcRenderer.invoke('merchant:copy-text', text),
  minimize: () => ipcRenderer.invoke('merchant:minimize'),
}));
