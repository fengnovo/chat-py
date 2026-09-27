import assert from 'node:assert/strict';
import test from 'node:test';
import { DEFAULT_DEVELOPMENT_URL, resolveWebUrl } from '../src/config.js';

test('uses localhost as the development default', () => {
  assert.equal(DEFAULT_DEVELOPMENT_URL, 'http://localhost:3000');
  assert.equal(resolveWebUrl(undefined, false).href, 'http://localhost:3000/');
});

test('normalizes a configured HTTPS URL', () => {
  assert.equal(resolveWebUrl(' https://chat.example.com/app ', true).href, 'https://chat.example.com/app');
});

test('requires an explicit URL for a packaged app', () => {
  assert.throws(() => resolveWebUrl(undefined, true), /ELECTRON_WEB_URL/);
});

test('uses the packaged URL embedded by the desktop builder', () => {
  assert.equal(
    resolveWebUrl(undefined, true, 'https://chat.example.com').href,
    'https://chat.example.com/',
  );
});

test('uses the configured local Web port when no explicit URL is set', () => {
  assert.equal(
    resolveWebUrl(undefined, false, 'http://localhost:3020').href,
    'http://localhost:3020/',
  );
});

test('rejects non HTTP(S) protocols', () => {
  assert.throws(() => resolveWebUrl('file:///tmp/index.html', false), /http/);
});

test('rejects URLs with embedded credentials', () => {
  assert.throws(() => resolveWebUrl('https://user:secret@example.com', false), /credentials/);
});
