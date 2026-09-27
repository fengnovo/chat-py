import assert from 'node:assert/strict';
import test from 'node:test';

import {
  PART_SIZE,
  clipboardImageFileName,
  imageFilesFromClipboard,
  isGzipCompressible,
  mapWithConcurrency,
  partCountForSize,
  withRetry,
} from '../app/components/resilient-chat/upload-pipeline';

test('isGzipCompressible matches text mime and text-like extensions only', () => {
  assert.equal(isGzipCompressible('notes.txt', 'text/plain'), true);
  assert.equal(isGzipCompressible('blob', 'text/markdown'), true);
  assert.equal(isGzipCompressible('data.json', 'application/octet-stream'), true);
  assert.equal(isGzipCompressible('app.js', 'application/octet-stream'), true);
  assert.equal(isGzipCompressible('logo.SVG', 'image/svg+xml'), true);
  // 已是压缩格式 / 二进制类型不重复压缩。
  assert.equal(isGzipCompressible('photo.png', 'image/png'), false);
  assert.equal(isGzipCompressible('report.pdf', 'application/pdf'), false);
  assert.equal(isGzipCompressible('archive.zip', 'application/zip'), false);
  assert.equal(isGzipCompressible('Makefile', 'application/octet-stream'), false);
});

test('partCountForSize rounds up and always reports at least one part', () => {
  assert.equal(partCountForSize(0), 1);
  assert.equal(partCountForSize(1), 1);
  assert.equal(partCountForSize(PART_SIZE), 1);
  assert.equal(partCountForSize(PART_SIZE + 1), 2);
  assert.equal(partCountForSize(PART_SIZE * 3 + PART_SIZE / 2), 4);
  assert.equal(partCountForSize(25, 10), 3);
});

test('mapWithConcurrency keeps order and bounds parallelism', async () => {
  assert.deepEqual(await mapWithConcurrency([], 2, async (v) => v), []);

  let active = 0;
  let maxActive = 0;
  const results = await mapWithConcurrency([1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await Promise.resolve();
    active -= 1;
    return value * 10;
  });
  assert.deepEqual(
    results.map((r) => (r.status === 'fulfilled' ? r.value : null)),
    [10, 20, 30, 40, 50],
  );
  assert.ok(maxActive <= 2, `并发超过上限：${maxActive}`);
});

test('mapWithConcurrency isolates rejections within the batch', async () => {
  const results = await mapWithConcurrency([1, 2, 3], 3, async (value) => {
    if (value === 2) throw new Error('boom');
    return value;
  });
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[1].status, 'rejected');
  assert.equal(results[2].status, 'fulfilled');
});

test('withRetry retries transient failures and surfaces the last error', async () => {
  let attempts = 0;
  const result = await withRetry(async () => {
    attempts += 1;
    if (attempts < 3) throw new Error(`fail-${attempts}`);
    return 'ok';
  }, 3);
  assert.equal(result, 'ok');
  assert.equal(attempts, 3);

  await assert.rejects(
    withRetry(
      async () => {
        throw new Error('always fails');
      },
      2,
    ),
    /always fails/,
  );
});

test('withRetry never retries user aborts', async () => {
  let attempts = 0;
  const abortError = new Error('cancel');
  abortError.name = 'AbortError';
  await assert.rejects(
    withRetry(async () => {
      attempts += 1;
      throw abortError;
    }, 3),
    /cancel/,
  );
  assert.equal(attempts, 1);
});

test('clipboardImageFileName follows clipboard-YYYYMMDD-HHMMSS.png', () => {
  assert.equal(
    clipboardImageFileName(new Date(2026, 2, 4, 9, 5, 7)),
    'clipboard-20260304-090507.png',
  );
});

function fakeDataTransfer(
  entries: Array<{ kind: string; type: string; file: File | null }>,
): DataTransfer {
  const items = entries.map((entry) => ({
    kind: entry.kind,
    type: entry.type,
    getAsFile: () => entry.file,
  }));
  return { items } as unknown as DataTransfer;
}

test('imageFilesFromClipboard extracts only image files', () => {
  assert.deepEqual(imageFilesFromClipboard(null), []);

  const image = new File([new Uint8Array([1, 2, 3])], 'shot.png', { type: 'image/png' });
  const other = new File([new Uint8Array([4])], 'note.txt', { type: 'text/plain' });
  const transfer = fakeDataTransfer([
    { kind: 'string', type: 'text/plain', file: null },
    { kind: 'file', type: 'text/plain', file: other },
    { kind: 'file', type: 'image/png', file: image },
    { kind: 'file', type: 'image/gif', file: null },
  ]);

  const files = imageFilesFromClipboard(transfer);
  assert.equal(files.length, 1);
  assert.equal(files[0], image);
});
