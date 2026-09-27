import assert from 'node:assert/strict';
import test from 'node:test';
import { resolveLocalWebUrl, shouldStartLocalServices } from '../src/local-runtime.js';

test('uses the configured local Web port', () => {
  assert.equal(resolveLocalWebUrl('3020').href, 'http://localhost:3020/');
});

test('falls back to port 3000 when no local port is configured', () => {
  assert.equal(resolveLocalWebUrl(undefined).href, 'http://localhost:3000/');
});

test('rejects invalid local ports', () => {
  assert.throws(() => resolveLocalWebUrl('0'), /PORT/);
  assert.throws(() => resolveLocalWebUrl('not-a-port'), /PORT/);
});

test('starts local services unless a remote URL or explicit skip is set', () => {
  assert.equal(shouldStartLocalServices({}), true);
  assert.equal(shouldStartLocalServices({ ELECTRON_WEB_URL: 'https://chat.example.com' }), false);
  assert.equal(shouldStartLocalServices({ DESKTOP_SKIP_LOCAL_SERVICES: '1' }), false);
});
