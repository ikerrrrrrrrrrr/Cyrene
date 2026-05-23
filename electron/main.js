const { app, BrowserWindow, dialog } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');

const isDev = process.env.ELECTRON_DEV === '1';
const isWindows = process.platform === 'win32';

let mainWindow = null;
let pythonProcess = null;
let pendingPortResolve = null;
let isShuttingDown = false;

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
    // Dev mode: use system python with the --web entry point
    return [
      path.join(__dirname, '..', 'src', 'cyrene', 'local_cli.py'),
      '--web',
      '--electron-mode',
    ];
  }
  // Frozen mode: trampoline with --launch-web + --electron
  return ['--launch-web', '--electron'];
}

function spawnPython() {
  const binaryPath = getPythonBinaryPath();
  const args = getPythonArgs();
  const cwd = isDev ? path.join(__dirname, '..') : undefined;

  if (binaryPath) {
    pythonProcess = spawn(binaryPath, args, {
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } else {
    pythonProcess = spawn('python3', args, {
      stdio: ['ignore', 'pipe', 'pipe'],
      cwd: cwd,
    });
  }

  let port = null;

  pythonProcess.stdout.on('data', (data) => {
    const text = data.toString();
    // Scan each line for PORT=<number>
    const match = text.match(/^PORT=(\d+)$/m);
    if (match) {
      port = parseInt(match[1], 10);
      if (pendingPortResolve) {
        pendingPortResolve(port);
        pendingPortResolve = null;
      }
    }
    // Log any other stdout for debugging
    process.stdout.write(`[cyrene] ${text}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    process.stderr.write(`[cyrene] ${data.toString()}`);
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
    app.quit();
  });

  pythonProcess.on('exit', (code) => {
    console.log(`[electron] Python backend exited (code=${code})`);
    pythonProcess = null;
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
      if (mainWindow && !mainWindow.isDestroyed()) {
        // Window still open — Python crashed.  Show error and quit.
        dialog.showErrorBox(
          'Cyrene - Backend Error',
          `The Python backend stopped unexpectedly (exit code ${code}).\n`
          + 'The application will now close.'
        );
      }
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
// Window management
// ---------------------------------------------------------------------------

async function createMainWindow() {
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

  mainWindow = new BrowserWindow({
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
      sandbox: true,
    },
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('close', () => {
    killPython();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Navigate to the local Python server
  const url = `http://127.0.0.1:${port}`;
  mainWindow.loadURL(url);
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    spawnPython();
    createMainWindow();
  });

  app.on('window-all-closed', () => {
    killPython();
    if (process.platform !== 'darwin') {
      app.quit();
    }
  });

  app.on('before-quit', () => {
    killPython();
  });

  app.on('activate', () => {
    // macOS: re-create window when dock icon is clicked and no windows exist
    if (mainWindow === null) {
      spawnPython();
      createMainWindow();
    }
  });
}
