/**
 * Schedule work after the current call stack without requiring the native
 * queueMicrotask API. Older browsers fall back to a Promise or a timer.
 */
export function scheduleMicrotask(callback: () => void): void {
  if (typeof globalThis !== 'undefined' && typeof globalThis.queueMicrotask === 'function') {
    globalThis.queueMicrotask(callback);
    return;
  }
  if (typeof Promise !== 'undefined') {
    void Promise.resolve().then(callback);
    return;
  }
  setTimeout(callback, 0);
}
