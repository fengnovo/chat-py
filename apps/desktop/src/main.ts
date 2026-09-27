import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { app, BrowserWindow, dialog, shell } from 'electron';
import { resolveWebUrl } from './config.js';
import { createLoadErrorGate, type LoadErrorGate } from './load-error.js';
import { resolveLocalWebUrl } from './local-runtime.js';
import { classifyNavigation } from './navigation.js';
import { createWindowOptions } from './window-options.js';

const RETRY_URL = 'keen-ai-retry:';

let configuredUrl: URL | undefined;
let mainWindow: BrowserWindow | undefined;

function openExternal(target: URL): void {
  if (target.protocol !== 'http:' && target.protocol !== 'https:') {
    return;
  }

  void shell.openExternal(target.href).catch((error: unknown) => {
    console.error('Unable to open external URL', error);
  });
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

async function showLoadError(window: BrowserWindow, reason: string, gate: LoadErrorGate): Promise<void> {
  if (!gate.tryStart()) {
    return;
  }

  try {
    const templatePath = join(app.getAppPath(), 'assets', 'load-error.html');
    const target = configuredUrl?.href ?? 'unknown URL';
    const template = await readFile(templatePath, 'utf8');
    const html = template
      .replaceAll('{{TARGET_URL}}', escapeHtml(target))
      .replaceAll('{{ERROR_REASON}}', escapeHtml(reason));

    await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  } finally {
    gate.finish();
  }
}

async function loadConfiguredPage(window: BrowserWindow, gate: LoadErrorGate): Promise<void> {
  if (!configuredUrl) {
    return;
  }

  try {
    await window.loadURL(configuredUrl.href);
  } catch (error: unknown) {
    const reason = error instanceof Error ? error.message : String(error);
    await showLoadError(window, reason, gate);
  }
}

function installNavigationPolicy(window: BrowserWindow, gate: LoadErrorGate): void {
  window.webContents.setWindowOpenHandler(({ url }) => {
    try {
      openExternal(new URL(url));
    } catch {
      // Ignore malformed window.open URLs and keep them out of Electron.
    }
    return { action: 'deny' };
  });

  window.webContents.on('will-navigate', (event, url) => {
    if (url === RETRY_URL) {
      event.preventDefault();
      void loadConfiguredPage(window, gate);
      return;
    }

    if (!configuredUrl) {
      event.preventDefault();
      return;
    }

    let target: URL;
    try {
      target = new URL(url);
    } catch {
      event.preventDefault();
      return;
    }

    const decision = classifyNavigation(target, configuredUrl.origin);
    if (decision === 'allow') {
      return;
    }

    event.preventDefault();
    if (decision === 'external') {
      openExternal(target);
    }
  });
}

function createMainWindow(): BrowserWindow {
  const window = new BrowserWindow(createWindowOptions());
  const loadErrorGate = createLoadErrorGate();
  installNavigationPolicy(window, loadErrorGate);

  window.once('ready-to-show', () => {
    window.show();
  });

  window.webContents.on('did-fail-load', (_event, errorCode, errorDescription, _validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3) {
      return;
    }

    void showLoadError(window, `${errorDescription} (${errorCode})`, loadErrorGate).catch((error: unknown) => {
      console.error('Unable to show load error page', error);
    });
  });

  window.on('closed', () => {
    if (mainWindow === window) {
      mainWindow = undefined;
    }
  });

  void loadConfiguredPage(window, loadErrorGate);
  return window;
}

async function readPackagedWebUrl(): Promise<string | undefined> {
  if (!app.isPackaged) {
    return undefined;
  }

  try {
    const packagePath = join(app.getAppPath(), 'package.json');
    const packageJson = JSON.parse(await readFile(packagePath, 'utf8')) as { electronWebUrl?: unknown };
    return typeof packageJson.electronWebUrl === 'string' ? packageJson.electronWebUrl : undefined;
  } catch (error: unknown) {
    console.error('Unable to read the packaged Web URL', error);
    return undefined;
  }
}

async function startApplication(): Promise<void> {
  try {
    const fallbackUrl = app.isPackaged
      ? await readPackagedWebUrl()
      : resolveLocalWebUrl(process.env.PORT).href;
    configuredUrl = resolveWebUrl(process.env.ELECTRON_WEB_URL, app.isPackaged, fallbackUrl);
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    dialog.showErrorBox('Keen AI could not start', message);
    app.quit();
    return;
  }

  mainWindow = createMainWindow();
}

void app.whenReady().then(startApplication);

app.on('activate', () => {
  if (!mainWindow) {
    mainWindow = createMainWindow();
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
