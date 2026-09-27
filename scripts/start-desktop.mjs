import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { resolveLocalWebUrl, shouldStartLocalServices } from '../apps/desktop/dist/local-runtime.js';

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const workspaceRoot = dirname(scriptDirectory);
const desktopDirectory = join(workspaceRoot, 'apps', 'desktop');
const pnpmCommand = process.platform === 'win32' ? 'pnpm.cmd' : 'pnpm';
const environment = { ...process.env };
const localWebUrl = resolveLocalWebUrl(environment.PORT);
const webUrl = environment.ELECTRON_WEB_URL ?? localWebUrl.href;

let localServices;
let desktopProcess;
let stopping = false;

function terminateProcess(child) {
  if (!child?.pid) return;

  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], { stdio: 'ignore' });
    return;
  }

  try {
    process.kill(-child.pid, 'SIGTERM');
  } catch {
    try { process.kill(child.pid, 'SIGTERM'); } catch { /* already exited */ }
  }
}

function runCommand(command, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env: environment, stdio: 'inherit' });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} ${args.join(' ')} exited with ${signal ?? code}`));
    });
  });
}

function startLocalServices() {
  const child = spawn(pnpmCommand, ['dev'], {
    cwd: workspaceRoot,
    env: environment,
    detached: process.platform !== 'win32',
    stdio: 'inherit',
  });
  child.once('error', (error) => console.error('[desktop] local services error', error));
  child.once('exit', (code, signal) => {
    if (!stopping && desktopProcess) {
      console.error(`[desktop] local services exited with ${signal ?? code}`);
      desktopProcess.kill();
    }
  });
  return child;
}

async function waitForWeb(url, child) {
  const deadline = Date.now() + 120_000;
  let lastError = 'not reachable yet';
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`local services exited before Web was ready (${child.exitCode})`);
    }

    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1_000) });
      if (response.status < 500) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Web service did not become ready at ${url}: ${lastError}`);
}

async function stopAll(code = 0) {
  if (stopping) return;
  stopping = true;
  terminateProcess(desktopProcess);
  terminateProcess(localServices);
  process.exitCode = code;
}

async function main() {
  if (shouldStartLocalServices(environment)) {
    console.log('[desktop] starting local infrastructure');
    await runCommand(pnpmCommand, ['infra:up'], workspaceRoot);
    await runCommand(pnpmCommand, ['db:migrate'], workspaceRoot);
    await runCommand(pnpmCommand, ['db:seed'], workspaceRoot);
    localServices = startLocalServices();
    console.log(`[desktop] waiting for Web at ${webUrl}`);
    await waitForWeb(webUrl, localServices);
  }

  console.log(`[desktop] opening ${webUrl}`);
  desktopProcess = spawn(pnpmCommand, ['start'], {
    cwd: desktopDirectory,
    env: { ...environment, ELECTRON_WEB_URL: webUrl, ELECTRON_RUN_AS_NODE: '' },
    stdio: 'inherit',
  });
  desktopProcess.once('error', (error) => { console.error('[desktop] Electron error', error); void stopAll(1); });
  desktopProcess.once('exit', (code) => { void stopAll(code ?? 1); });
}

process.once('SIGINT', () => { void stopAll(130); });
process.once('SIGTERM', () => { void stopAll(143); });

main().catch((error) => {
  console.error('[desktop] startup failed:', error instanceof Error ? error.message : error);
  void stopAll(1);
});
