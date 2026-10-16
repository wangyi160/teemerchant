'use strict';
const {app, BrowserWindow, Menu, Tray, nativeImage, protocol, ipcMain, dialog, clipboard} = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');
const {Backend} = require('./bridge.cjs');
const HOME = 'teemerchant://app/';
const CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'";
protocol.registerSchemesAsPrivileged([{scheme: 'teemerchant', privileges: {standard: true, secure: true, supportFetchAPI: true}}]);
app.setAppUserModelId('org.teerouter.teemerchant');
// Native data is shared with the prior merchant workbench; browser caches are separate.
const local = process.env.LOCALAPPDATA;
const dataArg = process.argv.find(v => v.startsWith('--data='));
const directory = path.resolve(dataArg ? dataArg.slice(7) : path.join(local || app.getPath('userData'), 'teemerchant'));
app.setPath('userData', path.join(directory, 'desktop'));
app.setPath('crashDumps', path.join(directory, 'desktop', 'crashes'));
let window, tray, backend, quitting = false, stopped = false;
const haveLock = app.requestSingleInstanceLock();
if (!haveLock) app.quit();

function trusted(event) {
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url !== HOME) {
    throw Error('桌面窗口来源无效');
  }
}
function show() { if (window) { window.show(); if (window.isMinimized()) window.restore(); window.focus(); } }
function icon() { return nativeImage.createFromPath(path.join(__dirname, 'assets', 'icon.png')); }

async function start() {
  const assets = path.join(app.getAppPath(), 'static');
  const resources = app.isPackaged ? process.resourcesPath : path.join(app.getAppPath(), 'build', 'resources');
  const executable = path.join(resources, 'engine', 'teemerchant-engine.exe');
  const bundled = path.join(resources, 'transport', 'teeproxy.exe');
  backend = new Backend(executable, ['--data', directory, '--bundled-transport', bundled]);
  await backend.ready;
  protocol.handle('teemerchant', async request => {
    const url = new URL(request.url);
    if (url.protocol !== 'teemerchant:' || url.host !== 'app' || url.username || url.password || url.search || url.hash || request.method !== 'GET') return new Response('', {status: 403});
    const names = {'/': ['index.html'], '/app.css': ['app.css'], '/app.js': ['account-config.js', 'app.js', 'account-ui.js', 'revenue-ui.js', 'desktop-ui.js']};
    const files = names[url.pathname];
    if (!files) return new Response('', {status: 404});
    const body = (await Promise.all(files.map(name => fs.readFile(path.join(assets, name))))).join('\n');
    const mime = url.pathname === '/' ? 'text/html' : url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript';
    return new Response(body, {headers: {'Content-Type': mime+'; charset=utf-8', 'Content-Security-Policy': CSP, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}});
  });
  window = new BrowserWindow({width: 1380, height: 940, minWidth: 940, minHeight: 650, show: false,
    title: 'teemerchant · 商户工作台', icon: icon(), backgroundColor: '#f4f6f9',
    webPreferences: {preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false, contextIsolation: true,
      sandbox: true, webSecurity: true, webviewTag: false, spellcheck: false}});
  const session = window.webContents.session;
  session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.setPermissionCheckHandler(() => false);
  session.webRequest.onBeforeRequest((details, callback) => {
    callback({cancel: !details.url.startsWith(HOME) && !details.url.startsWith('blob:'+HOME)});
  });
  window.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  window.webContents.on('will-navigate', (event, url) => { if (url !== HOME) event.preventDefault(); });
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  // Downloaded import examples use a native save dialog, never an automatic file write.
  session.on('will-download', (_event, item) => item.setSaveDialogOptions({title:'保存账号导入示例', defaultPath:'merchant-accounts.example.json', filters:[{name:'JSON',extensions:['json']}]}));
  ipcMain.handle('merchant:request', async (event, method, route, body) => {
    trusted(event);
    try { return {ok:true, value:await backend.call(method, route, body)}; }
    catch (error) { return {ok:false, error:error.message}; }
  });
  ipcMain.handle('merchant:choose-file', async (event, kind) => {
    trusted(event);
    if (!['tdx_policy','snp_policy','transport'].includes(kind)) throw Error('不支持的文件类型');
    const result = await dialog.showOpenDialog(window, {title:kind==='transport'?'选择 TEE 验证组件':'选择已审核的 TEE 信任策略',
      properties:['openFile'], filters:[kind==='transport'?{name:'Windows 程序',extensions:['exe']}:{name:'JSON 策略',extensions:['json']}]});
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle('merchant:copy-text', (event, text) => { trusted(event); if (typeof text !== 'string' || text.length > 1024*1024) throw Error('复制内容过大'); clipboard.writeText(text); });
  ipcMain.handle('merchant:minimize', event => { trusted(event); window.hide(); });
  tray = new Tray(icon()); tray.setToolTip('teemerchant · 商户工作台');
  tray.setContextMenu(Menu.buildFromTemplate([{label:'打开商户工作台',click:show},{type:'separator'},{label:'退出',click:()=>app.quit()}]));
  tray.on('double-click', show); tray.on('click', show);
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label:'工作台',submenu:[{label:'显示窗口',click:show},{label:'最小化到托盘',click:()=>window.hide()},{type:'separator'},{label:'退出',accelerator:'Alt+F4',click:()=>app.quit()}]},
    {label:'编辑',submenu:[{role:'undo',label:'撤销'},{role:'redo',label:'重做'},{type:'separator'},{role:'cut',label:'剪切'},{role:'copy',label:'复制'},{role:'paste',label:'粘贴'},{role:'selectAll',label:'全选'}]},
    {label:'查看',submenu:[{role:'reload',label:'刷新界面'},{role:'resetZoom',label:'实际大小'},{role:'zoomIn',label:'放大'},{role:'zoomOut',label:'缩小'}]},
    {label:'帮助',submenu:[{label:'关于 teemerchant',click:()=>dialog.showMessageBox(window,{type:'info',title:'teemerchant',message:`teemerchant ${app.getVersion()}`,detail:'开源商户桌面工作台\nhttps://github.com/wangyi160/teemerchant\n关闭窗口后继续在托盘运行；选择「退出」结束。'})}]}
  ]));
  window.on('close', event => { if (!quitting) { event.preventDefault(); window.hide(); } });
  if (!process.argv.includes('--start-hidden')) window.once('ready-to-show', show);
  await window.loadURL(HOME);
  backend.on('unavailable', () => { if (!quitting) dialog.showMessageBox(window, {type:'error', message:'本地管理组件已退出', detail:'请退出并重新打开 teemerchant。已保存的本地账号资料保留；写入操作没有自动重发。'}); });
}

if (haveLock) {
  app.on('second-instance', show);
  app.whenReady().then(start).catch(async () => {
    await dialog.showMessageBox({type:'error', title:'teemerchant 无法启动', message:'无法打开商户工作台', detail:'请先关闭旧版商户工作台，并确认安装文件完整且当前 Windows 用户有权访问本地数据。'});
    app.quit();
  });
  app.on('window-all-closed', () => {});
  app.on('before-quit', event => {
    if (stopped) return;
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    if (tray) tray.destroy();
    if (window) window.hide();
    Promise.resolve(backend?.close()).finally(() => { stopped = true; app.quit(); });
  });
}
