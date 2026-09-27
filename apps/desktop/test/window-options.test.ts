import assert from 'node:assert/strict';
import test from 'node:test';
import { createWindowOptions } from '../src/window-options.js';

test('creates a hidden, constrained, isolated BrowserWindow', () => {
  const options = createWindowOptions();

  assert.equal(options.width, 1280);
  assert.equal(options.height, 800);
  assert.equal(options.minWidth, 960);
  assert.equal(options.minHeight, 640);
  assert.equal(options.show, false);
  assert.equal(options.webPreferences?.nodeIntegration, false);
  assert.equal(options.webPreferences?.contextIsolation, true);
  assert.equal(options.webPreferences?.sandbox, true);
  assert.equal(options.webPreferences?.preload, undefined);
});
