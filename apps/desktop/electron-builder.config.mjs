const webUrl = process.env.ELECTRON_WEB_URL?.trim();

if (!webUrl) {
  throw new Error('ELECTRON_WEB_URL is required when building a packaged desktop application.');
}

try {
  const parsed = new URL(webUrl);
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) {
    throw new Error('URL must be an http(s) URL without credentials.');
  }
} catch (error) {
  throw new Error(`ELECTRON_WEB_URL must be a valid public http(s) URL: ${error instanceof Error ? error.message : String(error)}`);
}

export default {
  appId: 'com.keenai.desktop',
  productName: 'Keen AI',
  directories: {
    output: 'release',
  },
  files: [
    'dist/**/*',
    'assets/load-error.html',
    'package.json',
  ],
  npmRebuild: false,
  extraMetadata: {
    electronWebUrl: webUrl,
  },
  mac: {
    target: ['dmg', 'zip'],
    icon: 'assets/icon.icns',
    identity: process.env.CSC_LINK ? undefined : null,
    hardenedRuntime: true,
    gatekeeperAssess: false,
  },
  win: {
    target: ['nsis', 'zip'],
    icon: 'assets/icon.ico',
  },
  nsis: {
    oneClick: false,
    allowToChangeInstallationDirectory: true,
  },
};
