const DEFAULT_LOCAL_PORT = 3000;

export function resolveLocalWebUrl(rawPort: string | undefined): URL {
  const port = rawPort ? Number(rawPort) : DEFAULT_LOCAL_PORT;
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error('PORT must be a valid TCP port for the local Web service.');
  }

  return new URL(`http://localhost:${port}/`);
}

export function shouldStartLocalServices(env: Record<string, string | undefined>): boolean {
  return !env.ELECTRON_WEB_URL && env.DESKTOP_SKIP_LOCAL_SERVICES !== '1';
}
