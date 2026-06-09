const { app, BrowserWindow, dialog, ipcMain, Notification, session, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');

// Log file for Python backend output — written to os.tmpdir() so it survives
// app crashes and is easy to find even without a terminal window.
// On Windows with console=False the process has no console; writing from the
// Node side ensures the log is populated on every platform.
const ERROR_LOG_PATH = path.join(os.tmpdir(), 'cyrene_error.log');
let _errorLogStream = null;

function getErrorLogStream() {
  if (!_errorLogStream) {
    try {
      _errorLogStream = fs.createWriteStream(ERROR_LOG_PATH, { flags: 'a' });
    } catch (_) {}
  }
  return _errorLogStream;
}

function appendErrorLog(text) {
  const s = getErrorLogStream();
  if (s) s.write(text);
}

// Desktop-local auth token. Generated once at module load and shared with the
// Python backend via env (CYRENE_AUTH_TOKEN). Injected as the X-Cyrene-Token
// header on every request to the local backend (see installAuthHeaderInjector).
// The renderer never sees this token.
const AUTH_TOKEN = require('crypto').randomBytes(32).toString('hex');

const isDev = process.env.ELECTRON_DEV === '1';
const isWindows = process.platform === 'win32';
const supportsLoginItem = process.platform === 'darwin' || process.platform === 'win32';

let mainWindow = null;
let pythonProcess = null;
let pendingPortResolve = null;
let backendPort = null;
let backendUiMode = null;
let isShuttingDown = false;
let isQuitting = false;
let launchHidden = process.argv.includes('--hidden');

const DEFAULT_DESKTOP_SETTINGS = Object.freeze({
  launchAtLogin: false,
  runInBackground: false,
});

function getDesktopSettingsPath() {
  return path.join(app.getPath('userData'), 'desktop_settings.json');
}

function readDesktopSettings() {
  try {
    const raw = fs.readFileSync(getDesktopSettingsPath(), 'utf8');
    const parsed = JSON.parse(raw);
    return {
      launchAtLogin: parsed.launchAtLogin === true,
      runInBackground: parsed.runInBackground === true,
    };
  } catch (_) {
    return { ...DEFAULT_DESKTOP_SETTINGS };
  }
}

function writeDesktopSettings(settings) {
  const payload = {
    launchAtLogin: settings.launchAtLogin === true,
    runInBackground: settings.runInBackground === true,
  };
  fs.mkdirSync(path.dirname(getDesktopSettingsPath()), { recursive: true });
  fs.writeFileSync(getDesktopSettingsPath(), JSON.stringify(payload, null, 2), 'utf8');
}

function applyLaunchAtLogin(enabled) {
  if (!supportsLoginItem) return false;
  app.setLoginItemSettings({
    openAtLogin: enabled === true,
    openAsHidden: enabled === true,
    args: enabled === true ? ['--hidden'] : [],
  });
  return true;
}

function getDesktopSettings() {
  const stored = readDesktopSettings();
  return {
    ...stored,
    supportsLaunchAtLogin: supportsLoginItem,
    platform: process.platform,
  };
}

function saveDesktopSettings(updates) {
  const next = {
    ...readDesktopSettings(),
    ...updates,
  };
  writeDesktopSettings(next);
  applyLaunchAtLogin(next.launchAtLogin);
  return getDesktopSettings();
}

// ---------------------------------------------------------------------------
// Python child process management
// ---------------------------------------------------------------------------

function getPythonBinaryPath() {
  if (isDev) {
    return null; // use system python
  }
  // In a packaged Electron app, extraResources are in process.resourcesPath
  const base = process.resourcesPath;
  const name = isWindows ? 'Cyrene.exe' : 'Cyrene';
  return path.join(base, 'python-bundle', name);
}

function getPythonArgs() {
  if (isDev) {
    // Dev mode: use system python. CYRENE_UI_MODE=agent launches the legacy UI
    // (for testing the native title bar); anything else uses the workbench.
    const uiFlag = process.env.CYRENE_UI_MODE === 'agent' ? '--agent' : '--workbench';
    return [
      path.join(__dirname, '..', 'src', 'cyrene', 'local_cli.py'),
      uiFlag,
      '--electron-mode',
    ];
  }
  // Frozen mode: trampoline with --launch-web + --electron
  return ['--launch-web', '--electron'];
}

function spawnPython() {
  if (pythonProcess) return;
  const binaryPath = getPythonBinaryPath();
  const args = getPythonArgs();
  const cwd = isDev ? path.join(__dirname, '..') : undefined;
  const childEnv = {
    ...process.env,
    CYRENE_APP_EXECUTABLE: app.getPath('exe'),
    CYRENE_AUTH_TOKEN: AUTH_TOKEN,
  };

  if (binaryPath) {
    pythonProcess = spawn(binaryPath, args, {
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      env: childEnv,
    });
  } else {
    pythonProcess = spawn('python3', args, {
      stdio: ['ignore', 'pipe', 'pipe'],
      cwd: cwd,
      env: childEnv,
    });
  }

  let port = null;

  pythonProcess.stdout.on('data', (data) => {
    const text = data.toString();
    // Capture the UI mode (printed just before PORT) so the window is created
    // with the matching title bar style.
    const modeMatch = text.match(/^UIMODE=(\w+)$/m);
    if (modeMatch) {
      backendUiMode = modeMatch[1];
    }
    // Scan each line for PORT=<number>
    const match = text.match(/^PORT=(\d+)$/m);
    if (match) {
      port = parseInt(match[1], 10);
      // Store globally so a later waitForPort() can resolve even if the
      // PORT event arrived before any window registered a pending resolver
      // (e.g. launch-at-login hidden startup).
      backendPort = port;
      if (pendingPortResolve) {
        pendingPortResolve(port);
        pendingPortResolve = null;
      }
    }
    // Log any other stdout for debugging
    process.stdout.write(`[cyrene] ${text}`);
    appendErrorLog(text);
  });

  pythonProcess.stderr.on('data', (data) => {
    const text = data.toString();
    process.stderr.write(`[cyrene] ${text}`);
    appendErrorLog(text);
  });

  pythonProcess.on('error', (err) => {
    console.error('[electron] Failed to start Python backend:', err.message);
    dialog.showErrorBox(
      'Cyrene - Startup Error',
      `Failed to start the Python backend.\n\n${err.message}\n\n`
        + (isDev
          ? 'Make sure Python 3 is installed and accessible as "python3".'
          : 'The application may be corrupted. Please reinstall.')
    );
    if (pendingPortResolve) {
      pendingPortResolve(null);
      pendingPortResolve = null;
    }
    backendPort = null;
    app.quit();
  });

  pythonProcess.on('exit', (code) => {
    console.log(`[electron] Python backend exited (code=${code})`);
    pythonProcess = null;
    backendPort = null;
    if (code === 42) {
      // Exit code 42 = intentional restart after update.
      // Exit immediately to release the single-instance lock so the
      // detached updater script can launch the new version.
      app.exit(0);
    } else if (isShuttingDown) {
      // Normal shutdown — Python handled SIGTERM gracefully and exited with
      // code 0.  Don't scare the user with a crash dialog.
      app.quit();
    } else {
      // Show error regardless of window state — if Python crashed before
      // printing PORT= the window doesn't exist yet and the user would see
      // a silent flash-quit without this unconditional dialog.
      dialog.showErrorBox(
        'Cyrene - Backend Error',
        `The Python backend stopped unexpectedly (exit code ${code}).\n`
        + 'The application will now close.\n\n'
        + `If this keeps happening, check cyrene_error.log in ${os.tmpdir()}`
      );
      app.quit();
    }
  });
}

function killPython() {
  if (!pythonProcess) return;
  isShuttingDown = true;
  const proc = pythonProcess;
  pythonProcess = null;

  try {
    if (isWindows) {
      // On Windows, SIGTERM doesn't exist — use taskkill for the process tree.
      spawn('taskkill', ['/pid', String(proc.pid), '/f', '/t'], {
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      });
    } else {
      proc.kill('SIGTERM');
      // Graceful shutdown: wait up to 5s, then force-kill
      setTimeout(() => {
        try {
          if (proc.exitCode === null) proc.kill('SIGKILL');
        } catch (_) { /* ignore */ }
      }, 5000);
    }
  } catch (_) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Wait for Python to report its port
// ---------------------------------------------------------------------------

function waitForPort(timeoutMs = 30000) {
  // Port already reported (event may have arrived before this call) — resolve now.
  if (backendPort !== null) {
    return Promise.resolve(backendPort);
  }
  return new Promise((resolve, reject) => {
    pendingPortResolve = resolve;
    setTimeout(() => {
      if (pendingPortResolve) {
        pendingPortResolve = null;
        reject(new Error('Timed out waiting for Python backend to start'));
      }
    }, timeoutMs);
  });
}

// ---------------------------------------------------------------------------
// Auth header injection
// ---------------------------------------------------------------------------

// Inject the shared X-Cyrene-Token header on every request to the local
// backend — document loads, fetch, SSE, and WebSocket upgrades all go through
// onBeforeSendHeaders. Must be registered BEFORE the window loads the URL.
function installAuthHeaderInjector() {
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: ['http://127.0.0.1:*/*', 'ws://127.0.0.1:*/*'] },
    (details, callback) => {
      const requestHeaders = { ...details.requestHeaders, 'X-Cyrene-Token': AUTH_TOKEN };
      callback({ requestHeaders });
    }
  );

  // Deny all permission requests (camera, microphone, geolocation, etc.)
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });
}

// ---------------------------------------------------------------------------
// Window management
// ---------------------------------------------------------------------------

async function createMainWindow(shellOverride) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
    mainWindow.focus();
    return;
  }

  let port;
  try {
    port = await waitForPort();
  } catch (err) {
    dialog.showErrorBox(
      'Cyrene - Startup Timeout',
      'The Python backend did not start within 30 seconds.\n\n'
      + 'Check cyrene_error.log in your temp directory for details.'
    );
    killPython();
    app.quit();
    return;
  }

  if (!port) {
    // Error already handled in spawnPython (port resolve returned null)
    return;
  }

  // The workbench draws its own top bar and reserves room for the traffic
  // lights, so it uses the frameless inset title bar. The legacy/agent UI has a
  // normal top bar that the inset controls would overlap — keep the native
  // (default) title bar there. Unknown mode falls back to the workbench style.
  const uiShell = shellOverride || backendUiMode || 'workbench';
  const isLegacyShell = uiShell === 'legacy' || uiShell === 'agent';
  const useInsetTitleBar = !isLegacyShell;
  const windowOptions = {
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: 'Cyrene',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  };
  if (useInsetTitleBar) {
    windowOptions.titleBarStyle = 'hidden';
    windowOptions.trafficLightPosition = { x: 12, y: 23 };
  }
  mainWindow = new BrowserWindow(windowOptions);

  mainWindow.once('ready-to-show', () => {
    if (!launchHidden) {
      mainWindow.show();
    }
    if (isDev) {
      mainWindow.webContents.openDevTools();
    }
  });

  mainWindow.on('close', (event) => {
    const desktopSettings = readDesktopSettings();
    if (desktopSettings.runInBackground && !isQuitting) {
      event.preventDefault();
      mainWindow.hide();
      return;
    }
    killPython();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Navigate to the local Python server. The legacy/agent UI is selected via
  // the ?shell=legacy param so it renders in this (natively-framed) window even
  // when the backend was launched in workbench mode.
  const url = isLegacyShell
    ? `http://127.0.0.1:${port}/?shell=legacy`
    : `http://127.0.0.1:${port}`;
  // Force clear cache so the app always loads fresh assets
  mainWindow.webContents.session.clearCache();
  mainWindow.loadURL(url);

  // Restrict navigation to the local backend — block any attempt to leave 127.0.0.1:<port>
  mainWindow.webContents.on('will-navigate', (event, navigationUrl) => {
    try {
      const target = new URL(navigationUrl);
      if (target.hostname !== '127.0.0.1' || target.port !== String(port)) {
        event.preventDefault();
      }
    } catch (_) {
      event.preventDefault();
    }
  });

  // Control popup window creation from the renderer:
  // - local backend URLs: allow (image previews, attachments)
  // - external http/https: open in system browser via shell
  // - everything else (file://, data:, …): deny
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const target = new URL(url);
      if (target.hostname === '127.0.0.1' && target.port === String(port)) {
        return { action: 'allow' };
      }
      if (target.protocol === 'https:' || target.protocol === 'http:') {
        shell.openExternal(url);
      }
    } catch (_) {
      // malformed URL — fall through to deny
    }
    return { action: 'deny' };
  });
}

// Swap the window to a different UI shell at runtime (e.g. the workbench's
// "旧界面" button). titleBarStyle is fixed at creation, so we build a fresh
// window with the right chrome and discard the old one. The new window is
// created BEFORE the old is destroyed, so the window count never hits zero
// (which would fire window-all-closed → killPython). Returning to the new UI
// is a normal app restart.
let isSwitchingShell = false;
async function reopenWindowForShell(uiShell) {
  if (isSwitchingShell) return;
  isSwitchingShell = true;
  try {
    const old = mainWindow;
    const bounds = old && !old.isDestroyed() ? old.getBounds() : null;
    if (old && !old.isDestroyed()) {
      // Drop lifecycle listeners so destroying the old window doesn't
      // hide-to-background or kill the (still-needed) Python backend.
      old.removeAllListeners('close');
      old.removeAllListeners('closed');
    }
    mainWindow = null;
    await createMainWindow(uiShell);
    if (bounds && mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.setBounds(bounds);  // keep the same size/position across the swap
    }
    if (old && !old.isDestroyed()) {
      old.destroy();
    }
  } finally {
    isSwitchingShell = false;
  }
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    launchHidden = false;
    if (mainWindow) {
      if (!mainWindow.isVisible()) mainWindow.show();
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    } else {
      spawnPython();
      createMainWindow();
    }
  });

  app.whenReady().then(() => {
    installAuthHeaderInjector();
    applyLaunchAtLogin(readDesktopSettings().launchAtLogin);
    ipcMain.handle('desktop-settings:get', () => getDesktopSettings());
    ipcMain.handle('desktop-settings:update', (_event, updates) => saveDesktopSettings(updates || {}));
    ipcMain.handle('notification:show', (_event, { title, body }) => {
      new Notification({ title, body }).show();
    });
    ipcMain.handle('window:switch-shell', (_event, mode) => {
      const target = (mode === 'legacy' || mode === 'agent') ? 'legacy' : 'workbench';
      return reopenWindowForShell(target);
    });
    spawnPython();
    if (!launchHidden) {
      createMainWindow();
    }
  });

  app.on('window-all-closed', () => {
    killPython();
    if (process.platform !== 'darwin') {
      app.quit();
    }
  });

  app.on('before-quit', () => {
    isQuitting = true;
    killPython();
  });

  app.on('activate', () => {
    // macOS: re-create window when dock icon is clicked and no windows exist
    launchHidden = false;
    if (mainWindow === null) {
      spawnPython();
      createMainWindow();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}
