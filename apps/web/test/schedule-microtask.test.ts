import assert from 'node:assert/strict';
import test from 'node:test';

import { scheduleMicrotask } from '../app/lib/schedule-microtask';

test('scheduleMicrotask falls back when the native API is unavailable', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'queueMicrotask');
  try {
    Object.defineProperty(globalThis, 'queueMicrotask', {
      configurable: true,
      value: undefined,
      writable: true,
    });
    let called = false;
    scheduleMicrotask(() => { called = true; });
    assert.equal(called, false);
    await Promise.resolve();
    assert.equal(called, true);
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'queueMicrotask', descriptor);
    else delete (globalThis as { queueMicrotask?: unknown }).queueMicrotask;
  }
});
