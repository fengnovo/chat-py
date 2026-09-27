import assert from 'node:assert/strict';
import test from 'node:test';
import { createLoadErrorGate } from '../src/load-error.js';

test('prevents concurrent load-error page renders', () => {
  const gate = createLoadErrorGate();

  assert.equal(gate.tryStart(), true);
  assert.equal(gate.tryStart(), false);

  gate.finish();
  assert.equal(gate.tryStart(), true);
});
