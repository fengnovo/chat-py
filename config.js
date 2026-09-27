export const DEFAULT_DEVELOPMENT_URL = 'http://localhost:3000';
const SUPPORTED_PROTOCOLS = new Set(['http:', 'https:']);
export function resolveWebUrl(rawValue, isPackaged) {
    const value = rawValue?.trim() || (!isPackaged ? DEFAULT_DEVELOPMENT_URL : '');
    if (!value) {
        throw new Error('ELECTRON_WEB_URL is required for packaged applications.');
    }
    let url;
    try {
        url = new URL(value);
    }
    catch {
        throw new Error('ELECTRON_WEB_URL must be a valid http or https URL.');
    }
    if (!SUPPORTED_PROTOCOLS.has(url.protocol)) {
        throw new Error('ELECTRON_WEB_URL must use the http or https protocol.');
    }
    if (!url.hostname) {
        throw new Error('ELECTRON_WEB_URL must include a hostname.');
    }
    if (url.username || url.password) {
        throw new Error('ELECTRON_WEB_URL must not include credentials.');
    }
    return url;
}
