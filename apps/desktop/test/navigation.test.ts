import assert from 'node:assert/strict';
import test from 'node:test';
import { classifyNavigation } from '../src/navigation.js';

test('allows navigation within the configured Web origin', () => {
  assert.equal(
    classifyNavigation(new URL('https://chat.example.com/login'), 'https://chat.example.com'),
    'allow',
  );
});

test('classifies another HTTP(S) origin as external', () => {
  assert.equal(
    classifyNavigation(new URL('https://docs.example.com'), 'https://chat.example.com'),
    'external',
  );
});

test('denies dangerous protocols', () => {
  assert.equal(classifyNavigation(new URL('javascript:alert(1)'), 'https://chat.example.com'), 'deny');
});
