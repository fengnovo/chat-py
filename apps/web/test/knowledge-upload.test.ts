import assert from 'node:assert/strict';
import test from 'node:test';

import {
  requestComplete,
  requestInit,
  type PreparedPayload,
  type UploadTarget,
} from '../app/components/resilient-chat/upload-pipeline';

const checksum = '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function payloadOf(file: File, encoding?: 'gzip'): PreparedPayload {
  return {
    source: file,
    contentSha256: checksum,
    storedSha256: checksum,
    ...(encoding ? { contentEncoding: encoding } : {}),
  };
}

const documentTarget: UploadTarget = { kind: 'knowledge-document', kbId: 'kb-1' };

test('knowledge document init posts original + stored hashes and normalizes single mode', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const calls: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ input: String(input), init });
    return jsonResponse({
      mode: 'single',
      document: { id: 'document-1', name: 'notes.md', status: 'pending' },
      upload: {
        uploadUrl: 'https://storage.example/signed-upload',
        headers: { 'x-amz-meta-sha256': checksum },
      },
    }, 201);
  };

  const file = new File(['hello'], 'notes.md', { type: 'text/markdown' });
  const init = await requestInit(
    documentTarget,
    file,
    payloadOf(file),
    'text/markdown',
    new AbortController().signal,
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.input, '/api/knowledge-bases/kb-1/documents/uploads');
  assert.deepEqual(JSON.parse(String(calls[0]?.init?.body)), {
    name: 'notes.md',
    mime: 'text/markdown',
    sizeBytes: 5,
    sha256: checksum,
    storedSha256: checksum,
    storedSizeBytes: 5,
    kind: 'document',
  });
  assert.equal(init.mode, 'single');
  assert.equal(init.id, 'document-1');
  assert.equal(init.uploadUrl, 'https://storage.example/signed-upload');
  assert.deepEqual(init.headers, { 'x-amz-meta-sha256': checksum });
});

test('knowledge init forwards gzip encoding and accepts legacy upload.url shape', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const calls: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ input: String(input), init });
    return jsonResponse({
      mode: 'single',
      document: { id: 'gzipped' },
      upload: { url: 'https://storage.example/legacy-upload' },
    }, 201);
  };

  const file = new File(['hello'], 'notes.md', { type: 'text/markdown' });
  const init = await requestInit(
    { kind: 'knowledge-document', kbId: 'kb-1', directory: 'guides' },
    file,
    payloadOf(file, 'gzip'),
    'text/markdown',
    new AbortController().signal,
  );

  const body = JSON.parse(String(calls[0]?.init?.body));
  assert.equal(body.contentEncoding, 'gzip');
  assert.equal(body.directory, 'guides');
  assert.equal(init.uploadUrl, 'https://storage.example/legacy-upload');
});

test('knowledge asset init posts kind=asset with relPath', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const calls: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ input: String(input), init });
    return jsonResponse({ mode: 'instant', asset: { id: 'asset-1', name: 'logo.png' } }, 201);
  };

  const file = new File(['png-bytes'], 'logo.png', { type: 'image/png' });
  const init = await requestInit(
    { kind: 'knowledge-asset', kbId: 'kb-1', relPath: 'img/logo.png' },
    file,
    payloadOf(file),
    'image/png',
    new AbortController().signal,
  );

  const body = JSON.parse(String(calls[0]?.init?.body));
  assert.equal(body.kind, 'asset');
  assert.equal(body.relPath, 'img/logo.png');
  assert.equal(init.mode, 'instant');
  assert.equal(init.id, 'asset-1');
  assert.equal(init.uploadUrl, undefined);
});

test('knowledge document confirm posts stored size/hash and unwraps the document', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const calls: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ input: String(input), init });
    return jsonResponse({ id: 'document-1', status: 'queued' });
  };

  const file = new File(['hello'], 'notes.md', { type: 'text/markdown' });
  const result = (await requestComplete(
    documentTarget,
    { mode: 'single', id: 'document-1', result: null },
    payloadOf(file),
    new AbortController().signal,
  )) as { id: string; status: string };

  assert.equal(calls[0]?.input, '/api/knowledge-bases/kb-1/documents/document-1/confirm');
  assert.deepEqual(JSON.parse(String(calls[0]?.init?.body)), {
    sizeBytes: 5,
    sha256: checksum,
  });
  assert.equal(result.status, 'queued');
});

test('multipart confirm requires parts and forwards them', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const file = new File(['hello'], 'notes.md', { type: 'text/markdown' });
  await assert.rejects(
    requestComplete(
      documentTarget,
      { mode: 'multipart', id: 'document-1', result: null },
      payloadOf(file),
      new AbortController().signal,
    ),
    /分片数量不完整/,
  );

  const calls: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ input: String(input), init });
    return jsonResponse({ document: { id: 'document-1', status: 'queued' } });
  };
  const parts = [
    { number: 1, etag: 'etag-1' },
    { number: 2, etag: 'etag-2' },
  ];
  await requestComplete(
    documentTarget,
    { mode: 'multipart', id: 'document-1', result: null, completedParts: parts },
    payloadOf(file),
    new AbortController().signal,
  );
  assert.deepEqual(JSON.parse(String(calls[0]?.init?.body)).parts, parts);
});

test('chat complete only sends parts in multipart mode', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  const calls: Array<{ input: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ input: String(input), init });
    return jsonResponse({ attachment: { id: 'att-1' } });
  };

  const file = new File(['hello'], 'notes.md', { type: 'text/markdown' });
  const chat: UploadTarget = { kind: 'chat' };
  const result = await requestComplete(
    chat,
    { mode: 'single', id: 'att-1', result: null },
    payloadOf(file),
    new AbortController().signal,
  );

  assert.equal(calls[0]?.input, '/api/agent/chat-attachments/att-1/complete');
  assert.equal(calls[0]?.init?.body, undefined);
  assert.deepEqual(result, { id: 'att-1' });
});

test('init surfaces friendly messages for known server error codes', async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });

  globalThis.fetch = async () => jsonResponse({ error: 'knowledge_document_too_large' }, 400);
  const file = new File(['hello'], 'notes.md', { type: 'text/markdown' });
  await assert.rejects(
    requestInit(documentTarget, file, payloadOf(file), 'text/markdown', new AbortController().signal),
    /知识库大小限制/,
  );
});
