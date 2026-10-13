// GLASS — Electron main process: windows, shortcuts, tray, engine lifecycle.
const { app, BrowserWindow, ipcMain, globalShortcut, screen, Tray, Menu, nativeImage, dialog, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const net = require('net');
const crypto = require('crypto');
const { spawn } = require('child_process');

const DEV = !!process.env.GLASS_DEV;
const ROOT = path.join(__dirname, '..');
const ENGINE_DIR = app.isPackaged ? path.join(process.resourcesPath, 'engine') : path.join(ROOT, 'engine');
const IS_MAC = process.platform === 'darwin';
// per-user data: %LOCALAPPDATA%\GLASS on Windows, ~/Library/Application Support/GLASS on macOS
const LOCAL = IS_MAC ? app.getPath('appData') : (process.env.LOCALAPPDATA || app.getPath('userData'));
const PY_CANDIDATES = IS_MAC ? [
  path.join(ENGINE_DIR, '.venv', 'bin', 'python3'),
  path.join(LOCAL, 'GLASS', 'venv', 'bin', 'python3'),
] : [
  path.join(ENGINE_DIR, '.venv', 'Scripts', 'python.exe'),
  path.join(LOCAL, 'GLASS', 'venv', 'Scripts', 'python.exe'),
];
// Windows reports physical pixels, macOS points; the engine handles Retina scaling itself
const toScreenRect = (win, r) => (IS_MAC ? r : screen.dipToScreenRect(win, r));
const toScreenPoint = (p) => (IS_MAC ? p : screen.dipToScreenPoint(p));

if (!app.requestSingleInstanceLock()) app.quit();

// ------------------------------------------------------------------ UI settings
const UI_PATH = path.join(app.getPath('userData'), 'ui.json');
const DEFAULT_SHORTCUTS = {
  toggleOverlay: 'Control+Alt+G',
  focusContext: process.platform === 'darwin' ? 'Control+Alt+K' : 'Control+Alt+Space',
  expandAnswer: 'Control+Alt+A',
  showTask: 'Control+Alt+T',
  explain: 'Control+Alt+E',
  catchup: 'Control+Alt+M',
  snip: 'Control+Alt+S',
  openNotes: 'Control+Alt+N',
  pauseCapture: 'Control+Alt+P',
};
let ui = { overlay: null, opacity: 1, glass: 'acrylic', ghost: false, shortcuts: { ...DEFAULT_SHORTCUTS }, width: 380 };
try { ui = { ...ui, ...JSON.parse(fs.readFileSync(UI_PATH, 'utf8')) }; ui.shortcuts = { ...DEFAULT_SHORTCUTS, ...ui.shortcuts }; } catch {}
let saveTimer = null;
function saveUi() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { try { fs.writeFileSync(UI_PATH, JSON.stringify(ui, null, 2)); } catch {} }, 300);
}

// ------------------------------------------------------------------ engine
// GLASS_DEV_TOKEN lets the integration tests drive a running app; normal launches get a random per-run token
const TOKEN = (DEV || process.env.GLASS_TEST) && process.env.GLASS_DEV_TOKEN ? process.env.GLASS_DEV_TOKEN : crypto.randomBytes(24).toString('hex');
let port = 47821;
let engine = null;
let engineState = { state: 'starting', message: '' };
let quitting = false;
let backoff = 1000;
let engineLog = [];

function freePort(start) {
  return new Promise((resolve) => {
    const tryPort = (p) => {
      const srv = net.createServer();
      srv.once('error', () => tryPort(p + 1));
      srv.once('listening', () => srv.close(() => resolve(p)));
      srv.listen(p, '127.0.0.1');
    };
    tryPort(start);
  });
}

function broadcast(channel, data) {
  for (const w of BrowserWindow.getAllWindows()) if (!w.isDestroyed()) w.webContents.send(channel, data);
}

function setEngineState(state, message = '') {
  engineState = { state, message, port, token: TOKEN };
  broadcast('engine:state', engineState);
}

async function startEngine() {
  const py = PY_CANDIDATES.find((p) => fs.existsSync(p));
  if (!py) {
    setEngineState('missing', 'One-time setup downloads the local speech and AI models (about 8 GB). No account, key or payment needed.');
    return;
  }
  port = await freePort(47821);
  setEngineState('starting');
  engine = spawn(py, ['-m', 'glass', '--port', String(port)], {
    cwd: ENGINE_DIR,
    env: { ...process.env, GLASS_TOKEN: TOKEN, GLASS_PARENT_PID: String(process.pid), PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1', PYTHONPATH: ENGINE_DIR },
    // stdin must NOT be a pipe the engine blocks on: on Windows that stalls every child it spawns
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  const onOut = (d) => {
    const s = d.toString();
    engineLog.push(s);
    if (engineLog.length > 400) engineLog = engineLog.slice(-300);
    if (s.includes('GLASS_READY')) { backoff = 1000; setEngineState('ready'); }
  };
  engine.stdout.on('data', onOut);
  engine.stderr.on('data', onOut);
  engine.on('exit', (code) => {
    engine = null;
    if (quitting) return;
    setEngineState('restarting', `Engine stopped (code ${code}). Restarting…`);
    setTimeout(startEngine, backoff);
    backoff = Math.min(backoff * 2, 15000);
  });
}

function stopEngine() {
  if (!engine) return Promise.resolve();
  const e = engine;
  return new Promise((resolve) => {
    const done = () => { clearTimeout(timer); resolve(); };
    const timer = setTimeout(() => { try { e.kill(); } catch {} resolve(); }, 8000);
    e.once('exit', done);
    try {  // graceful: lets the engine save the last notes and release the GPU
      const ws = new WebSocket(`ws://127.0.0.1:${port}/?token=${TOKEN}`);
      ws.onopen = () => ws.send(JSON.stringify({ cmd: 'shutdown', rid: 1 }));
      ws.onerror = () => { try { e.kill(); } catch {} };
    } catch { try { e.kill(); } catch {} }
  });
}

// ------------------------------------------------------------------ windows
let overlay = null, dashboard = null, snipWin = null, tray = null;
const preload = path.join(__dirname, 'preload.cjs');

function pageUrl(name, hash = '') {
  if (DEV) return `http://localhost:5173/${name}.html${hash ? '#' + hash : ''}`;
  return `file://${path.join(ROOT, 'dist', `${name}.html`).replace(/\\/g, '/')}${hash ? '#' + hash : ''}`;
}

function createOverlay() {
  const wa = screen.getPrimaryDisplay().workArea;
  const pos = ui.overlay && screen.getDisplayMatching({ ...ui.overlay, width: 10, height: 10 })
    ? ui.overlay : { x: wa.x + wa.width - ui.width - 24, y: wa.y + 24 };
  const acrylic = ui.glass === 'acrylic';
  overlay = new BrowserWindow({
    x: pos.x, y: pos.y, width: ui.width, height: 132,
    frame: false, resizable: false, maximizable: false, minimizable: false, fullscreenable: false,
    skipTaskbar: true, alwaysOnTop: true, focusable: false, show: false, hasShadow: true,
    transparent: IS_MAC ? true : !acrylic,
    backgroundColor: '#00000000',
    backgroundMaterial: !IS_MAC && acrylic ? 'acrylic' : undefined,
    // macOS: native frosted glass, and a non-activating panel that floats over full-screen Meet
    vibrancy: IS_MAC && acrylic ? 'hud' : undefined,
    visualEffectState: IS_MAC ? 'active' : undefined,
    type: IS_MAC ? 'panel' : undefined,
    roundedCorners: true,
    title: 'GLASS',
    webPreferences: { preload, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false },
  });
  overlay.setAlwaysOnTop(true, 'screen-saver');
  if (IS_MAC) overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlay.setOpacity(Math.max(0.35, Math.min(1, ui.opacity || 1)));
  overlay.loadURL(pageUrl('overlay'));
  overlay.once('ready-to-show', () => overlay.showInactive());
  overlay.on('moved', () => {
    const b = overlay.getBounds();
    ui.overlay = { x: b.x, y: b.y };
    saveUi();
    sendOverlayBounds();
  });
  overlay.on('closed', () => { overlay = null; });
}

function sendOverlayBounds() {
  if (!overlay) return;
  const phys = toScreenRect(overlay, overlay.getBounds());
  overlay.webContents.send('overlay:bounds', [phys.x, phys.y, phys.width, phys.height]);
}

function openDashboard(view) {
  if (dashboard && !dashboard.isDestroyed()) {
    if (view) dashboard.webContents.send('dashboard:view', view);
    dashboard.show();
    dashboard.focus();
    return;
  }
  dashboard = new BrowserWindow({
    width: 1240, height: 820, minWidth: 900, minHeight: 600,
    title: 'GLASS',
    backgroundColor: '#00000000',
    ...(IS_MAC
      ? { vibrancy: 'under-window', visualEffectState: 'active', titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 14, y: 13 } }
      : { backgroundMaterial: 'mica', titleBarStyle: 'hidden', titleBarOverlay: { color: '#00000000', symbolColor: '#C9CEDA', height: 40 } }),
    show: false,
    icon: path.join(ROOT, 'build', 'icon.png'),
    webPreferences: { preload, contextIsolation: true, nodeIntegration: false },
  });
  dashboard.loadURL(pageUrl('dashboard', view || ''));
  dashboard.once('ready-to-show', () => dashboard.show());
  dashboard.on('closed', () => { dashboard = null; });
}

// Snip & Solve: freeze the screen first (like the Windows Snipping Tool). A full-screen
// selection window makes Chrome think Meet is hidden, so it stops drawing the cameras
// and screen share (they turn grey) — the frozen image shows what was really there.
let snipFreezing = false;
function startSnip() {
  if (snipWin || snipFreezing) return;
  const cursor = screen.getCursorScreenPoint();
  const phys = toScreenPoint(cursor);
  const overlayWasVisible = !!(overlay && overlay.isVisible());
  if (!overlay || engineState.state !== 'ready') return openSnipWindow(null, overlayWasVisible);
  snipFreezing = true;
  overlay.hide();  // keep GLASS itself out of the frozen image
  let done = false;
  const finish = (image) => {
    if (done) return;
    done = true;
    snipFreezing = false;
    ipcMain.removeListener('snip:frozen', onFrozen);
    openSnipWindow(image, overlayWasVisible);
  };
  const onFrozen = (_e, image) => finish(image);
  ipcMain.on('snip:frozen', onFrozen);
  setTimeout(() => overlay.webContents.send('snip:freeze', { x: phys.x, y: phys.y }), 120);
  setTimeout(() => finish(null), 3500);
}

function openSnipWindow(image, restoreOverlay) {
  if (snipWin) return;
  const d = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  snipWin = new BrowserWindow({
    ...d.bounds, frame: false, transparent: true, resizable: false, movable: false, skipTaskbar: true,
    alwaysOnTop: true, fullscreenable: false, hasShadow: false, show: false, backgroundColor: '#00000000',
    webPreferences: { preload, contextIsolation: true, nodeIntegration: false },
  });
  snipWin.setAlwaysOnTop(true, 'screen-saver');
  if (IS_MAC) snipWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  snipWin.loadURL(pageUrl('snip'));
  snipWin.webContents.once('did-finish-load', () => { if (image && snipWin) snipWin.webContents.send('snip:image', image); });
  snipWin.once('ready-to-show', () => { snipWin.show(); snipWin.focus(); });
  snipWin.on('closed', () => {
    snipWin = null;
    if (restoreOverlay && overlay && !overlay.isVisible()) overlay.showInactive();
  });
  snipWin._display = d;
}

// ------------------------------------------------------------------ shortcuts
function registerShortcuts() {
  globalShortcut.unregisterAll();
  const failed = [];
  for (const [action, accel] of Object.entries(ui.shortcuts)) {
    if (!accel) continue;
    try {
      const ok = globalShortcut.register(accel, () => onShortcut(action));
      if (!ok) failed.push(accel);
    } catch { failed.push(accel); }
  }
  return failed;
}

function onShortcut(action) {
  if (action === 'toggleOverlay') {
    if (!overlay) return createOverlay();
    // visible: the renderer decides (a minimised pill expands; a full panel hides)
    if (overlay.isVisible()) overlay.webContents.send('shortcut', 'toggleOverlay');
    else overlay.showInactive();
    return;
  }
  if (action === 'openNotes') return openDashboard('notes');
  if (action === 'snip') return startSnip();
  if (!overlay) createOverlay();
  if (!overlay.isVisible()) overlay.showInactive();
  if (action === 'focusContext') focusOverlay(true);
  overlay.webContents.send('shortcut', action);
}

function focusOverlay(on) {
  if (!overlay) return;
  if (on) {
    overlay.setFocusable(true);
    overlay.focus();
    overlay.webContents.focus();
  } else {
    overlay.setFocusable(false);
    overlay.blur();
  }
}

// ------------------------------------------------------------------ IPC
ipcMain.handle('engine:config', () => engineState);
ipcMain.handle('engine:log', () => engineLog.join(''));
ipcMain.handle('engine:restart', () => { if (engine) { engine.kill(); } else { startEngine(); } return true; });
ipcMain.handle('engine:setup', () => {
  // a visible terminal window: the student sees every download and install step
  if (IS_MAC) {
    const script = path.join(ENGINE_DIR, 'setup_engine.sh').replace(/"/g, '\\"');
    spawn('osascript', ['-e', `tell application "Terminal" to do script "bash \\"${script}\\""`, '-e', 'tell application "Terminal" to activate'],
      { detached: true, stdio: 'ignore' }).unref();
    return true;
  }
  const script = path.join(ENGINE_DIR, 'setup_engine.ps1');
  const p = spawn('cmd.exe', ['/c', 'start', '"GLASS setup"', 'powershell.exe', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', script],
    { detached: true, stdio: 'ignore', windowsHide: false });
  p.unref();
  return true;
});

ipcMain.on('overlay:resize', (_e, { width, height }) => {
  if (!overlay) return;
  const b = overlay.getBounds();
  const wa = screen.getDisplayMatching(b).workArea;
  const w = Math.round(width), h = Math.round(Math.min(height, wa.height - 40));
  let y = b.y;
  if (y + h > wa.y + wa.height - 8) y = Math.max(wa.y + 8, wa.y + wa.height - 8 - h);  // grow upwards near the bottom
  let x = b.x;
  if (w !== b.width && b.x + b.width >= wa.x + wa.width - 40) x = b.x + b.width - w;      // stay anchored to the right edge
  overlay.setBounds({ x, y, width: w, height: h }, false);
  if (w !== ui.width && w > 300) { ui.width = w; saveUi(); }
});
ipcMain.on('overlay:focus', (_e, on) => focusOverlay(!!on));
ipcMain.on('overlay:ignore-mouse', (_e, on) => overlay && overlay.setIgnoreMouseEvents(!!on, { forward: true }));
ipcMain.on('overlay:hide', () => overlay && overlay.hide());
ipcMain.on('dashboard:open', (_e, view) => openDashboard(view));
ipcMain.on('snip:start', () => startSnip());
ipcMain.on('snip:cancel', () => { if (snipWin) snipWin.close(); });
ipcMain.on('snip:vision', () => { if (overlay) overlay.webContents.send('snip:vision'); });
ipcMain.on('snip:done', (_e, r) => {
  if (!snipWin) return;
  const d = snipWin._display;
  const dip = { x: d.bounds.x + r.x, y: d.bounds.y + r.y, width: r.width, height: r.height };
  const phys = toScreenRect(null, dip);
  snipWin.close();
  // give the compositor a moment to remove the dimming layer before capture
  setTimeout(() => {
    if (!overlay) createOverlay();
    overlay.showInactive();
    overlay.webContents.send('snip:rect', { x: phys.x, y: phys.y, w: phys.width, h: phys.height, vision: !!r.vision });
  }, 180);
});

ipcMain.handle('ui:get', () => ({ ...ui, defaults: DEFAULT_SHORTCUTS }));
ipcMain.handle('ui:set', (_e, patch) => {
  const glassChanged = patch.glass && patch.glass !== ui.glass;
  ui = { ...ui, ...patch, shortcuts: { ...ui.shortcuts, ...(patch.shortcuts || {}) } };
  saveUi();
  if (overlay && patch.opacity != null) overlay.setOpacity(Math.max(0.35, Math.min(1, patch.opacity)));
  const failed = patch.shortcuts ? registerShortcuts() : [];
  if (glassChanged && overlay) { overlay.destroy(); createOverlay(); }
  else if ('overlay' in patch && patch.overlay === null && overlay) {
    const wa = screen.getPrimaryDisplay().workArea;
    const b = overlay.getBounds();
    overlay.setBounds({ ...b, x: wa.x + wa.width - b.width - 24, y: wa.y + 24 });
  }
  broadcast('ui:changed', ui);
  return { ui, failed };
});

ipcMain.handle('export:markdown', async (_e, { name, markdown }) => {
  const { canceled, filePath } = await dialog.showSaveDialog({ defaultPath: `${name}.md`, filters: [{ name: 'Markdown', extensions: ['md'] }] });
  if (canceled || !filePath) return null;
  fs.writeFileSync(filePath, markdown, 'utf8');
  return filePath;
});

ipcMain.handle('export:pdf', async (_e, { name, html }) => {
  const { canceled, filePath } = await dialog.showSaveDialog({ defaultPath: `${name}.pdf`, filters: [{ name: 'PDF', extensions: ['pdf'] }] });
  if (canceled || !filePath) return null;
  const w = new BrowserWindow({ show: false, webPreferences: { offscreen: true } });
  await w.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  const pdf = await w.webContents.printToPDF({ pageSize: 'A4', printBackground: true, margins: { marginType: 'default' } });
  fs.writeFileSync(filePath, pdf);
  w.destroy();
  shell.showItemInFolder(filePath);
  return filePath;
});

ipcMain.on('app:quit', () => app.quit());

// ------------------------------------------------------------------ tray
function createTray() {
  // macOS menu bar wants a monochrome "template" image that adapts to light/dark
  const icon = nativeImage.createFromPath(path.join(ROOT, 'build', IS_MAC ? 'trayTemplate.png' : 'tray.png'));
  if (IS_MAC) icon.setTemplateImage(true);
  tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon);
  tray.setToolTip('GLASS — classroom copilot');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show overlay', click: () => (overlay ? overlay.showInactive() : createOverlay()) },
    { label: 'Open dashboard', click: () => openDashboard() },
    { label: 'Snip & solve', click: () => startSnip() },
    { type: 'separator' },
    { label: 'Settings', click: () => openDashboard('settings') },
    { label: 'Quit GLASS', click: () => app.quit() },
  ]));
  tray.on('click', () => openDashboard());
}

// ------------------------------------------------------------------ app
// `GLASS.exe --view=notes` (e.g. from a Start-menu shortcut) opens that dashboard section
const viewArg = (argv) => { const a = argv.find((x) => x.startsWith('--view=')); return a ? a.slice(7) : undefined; };
app.on('second-instance', (_e, argv) => openDashboard(viewArg(argv)));
app.on('activate', () => openDashboard());  // macOS: clicking the Dock icon
app.whenReady().then(async () => {
  app.setAppUserModelId('app.glass.classcopilot');
  await startEngine();
  createOverlay();
  createTray();
  registerShortcuts();
  if (process.argv.includes('--dashboard') || viewArg(process.argv)) openDashboard(viewArg(process.argv));
});
app.on('window-all-closed', (e) => e.preventDefault?.());
app.on('before-quit', (ev) => {
  if (quitting) return;
  quitting = true;
  globalShortcut.unregisterAll();
  if (engine) {
    ev.preventDefault();
    stopEngine().then(() => app.quit());
  }
});
