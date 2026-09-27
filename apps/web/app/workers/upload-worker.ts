/// <reference lib="webworker" />
import { createSHA256 } from 'hash-wasm';
import * as pdfjs from 'pdfjs-dist';
import type { DocumentInitParameters } from 'pdfjs-dist/types/src/display/api';
// @ts-expect-error mammoth 预构建浏览器 UMD 包不带类型声明
import mammoth from 'mammoth/mammoth.browser.js';

import {
  isGzipCompressible,
  mapWithConcurrency,
  PART_MAX_RETRIES,
  partCountForSize,
  requestComplete,
  requestInit,
  UPLOAD_CONCURRENCY,
  withRetry,
  type InitResponse,
  type LocalDocumentPreview,
  type PreparedPayload,
  type UploadOptions,
  type UploadTarget,
  type WorkerInboundMessage,
  type WorkerOutboundMessage,
} from '../components/resilient-chat/upload-pipeline';

// 交给打包器产出静态资源 URL；嵌套 Worker 不可用时 pdfjs 会回退到
// 同 URL 的 fake worker（运行在本 Worker 线程内），不再多开进程。
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).href;

declare const self: DedicatedWorkerGlobalScope;

function post(message: WorkerOutboundMessage): void {
  self.postMessage(message);
}

function abortRejection(signal: AbortSignal): Promise<never> {
  return new Promise<never>((_, reject) => {
    if (signal.aborted) reject(new DOMException('Aborted', 'AbortError'));
    else {
      signal.addEventListener(
        'abort',
        () => reject(new DOMException('Aborted', 'AbortError')),
        { once: true },
      );
    }
  });
}

async function readChunk<T>(
  reader: ReadableStreamDefaultReader<T>,
  signal: AbortSignal,
): Promise<ReadableStreamReadResult<T>> {
  return Promise.race([reader.read(), abortRejection(signal)]);
}

// ── 0. 粘贴图片重编码压缩 ────────────────────────────────────────────

const MAX_IMAGE_DIMENSION = 2560;

function replaceExtension(name: string, extension: string): string {
  const base = name.includes('.')
    ? name.slice(0, name.lastIndexOf('.'))
    : name;
  return `${base}.${extension}`;
}

async function maybeRecompressImage(
  file: File,
  options: UploadOptions,
  signal: AbortSignal,
): Promise<{ file: File; changed: boolean }> {
  if (
    !options.recompressImage ||
    !file.type.startsWith('image/') ||
    file.type === 'image/gif'
  ) {
    return { file, changed: false };
  }
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    return { file, changed: false };
  }
  try {
    const scale = Math.min(
      1,
      MAX_IMAGE_DIMENSION / Math.max(bitmap.width, bitmap.height),
    );
    // JPEG 已高压缩且尺寸不过大：避免无谓重编码。
    if (scale === 1 && file.type === 'image/jpeg') {
      return { file, changed: false };
    }
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = new OffscreenCanvas(width, height);
    const context = canvas.getContext('2d');
    if (!context) return { file, changed: false };
    // 透明 PNG 转 JPEG 前铺白底，否则透明区域变黑。
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, width, height);
    context.drawImage(bitmap, 0, 0, width, height);

    const candidates: Array<[Blob, string]> = [];
    const webp = await canvas
      .convertToBlob({ type: 'image/webp', quality: 0.88 })
      .catch(() => null);
    if (webp) candidates.push([webp, 'webp']);
    const jpeg = await canvas.convertToBlob({
      type: 'image/jpeg',
      quality: 0.9,
    });
    candidates.push([jpeg, 'jpeg']);
    candidates.sort((a, b) => a[0].size - b[0].size);
    const [bestBlob, ext] = candidates[0];
    if (bestBlob.size >= file.size) return { file, changed: false };
    signal.throwIfAborted();
    const name = replaceExtension(
      file.name,
      ext === 'jpeg' ? 'jpg' : 'webp',
    );
    return {
      file: new File([bestBlob], name, { type: bestBlob.type }),
      changed: true,
    };
  } finally {
    bitmap.close();
  }
}

// ── 1. Wasm 哈希 + gzip 压缩 ─────────────────────────────────────────

async function preparePayload(
  file: File,
  options: UploadOptions,
  signal: AbortSignal,
  onProgress: (progress: number) => void,
): Promise<PreparedPayload> {
  const contentHasher = await createSHA256();
  contentHasher.init();
  const compress = options.gzip && isGzipCompressible(file.name, file.type);

  if (!compress) {
    const reader = file.stream().getReader();
    let processed = 0;
    while (true) {
      const { done, value } = await readChunk(reader, signal);
      if (done) break;
      contentHasher.update(value);
      processed += value.byteLength;
      onProgress(processed / file.size);
    }
    const digest = contentHasher.digest('hex');
    return { source: file, contentSha256: digest, storedSha256: digest };
  }

  const storedHasher = await createSHA256();
  storedHasher.init();
  const compression = new CompressionStream('gzip');
  const compressedChunks: Uint8Array[] = [];

  async function pump(): Promise<void> {
    const reader = file.stream().getReader();
    const writer = compression.writable.getWriter();
    let processed = 0;
    try {
      while (true) {
        const { done, value } = await readChunk(reader, signal);
        if (done) break;
        contentHasher.update(value);
        processed += value.byteLength;
        onProgress(processed / file.size);
        await writer.ready;
        await writer.write(value);
      }
      await writer.close();
    } finally {
      writer.releaseLock();
    }
  }

  async function drain(): Promise<void> {
    const reader = compression.readable.getReader();
    while (true) {
      const { done, value } = await readChunk(reader, signal);
      if (done) break;
      storedHasher.update(value);
      compressedChunks.push(value);
    }
  }

  await Promise.all([pump(), drain()]);
  const contentSha256 = contentHasher.digest('hex');
  const storedSha256 = storedHasher.digest('hex');
  let compressedSize = 0;
  for (const chunk of compressedChunks) compressedSize += chunk.byteLength;
  // 压缩反而变大（小文件/熵高）：回退原文直传。
  if (compressedSize >= file.size) {
    return { source: file, contentSha256, storedSha256: contentSha256 };
  }
  return {
    source: new Blob(compressedChunks as BlobPart[], {
      type: file.type || 'application/octet-stream',
    }),
    contentSha256,
    storedSha256,
    contentEncoding: 'gzip',
  };
}

// ── 3. 直传（XHR 以获得上传进度，支持中止）──────────────────────────

function putViaXhr(
  url: string,
  body: Blob,
  headers: Record<string, string>,
  signal: AbortSignal,
  onLoaded: (loaded: number) => void,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    for (const [key, value] of Object.entries(headers)) {
      if (key.toLowerCase() === 'content-length') continue;
      xhr.setRequestHeader(key, value);
    }
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onLoaded(event.loaded);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const etag = xhr.getResponseHeader('etag') ?? '';
        if (!etag) {
          reject(new Error('存储服务未返回分片标识'));
          return;
        }
        resolve(etag);
      } else {
        reject(new Error(`文件上传失败（HTTP ${xhr.status}）`));
      }
    };
    xhr.onerror = () => reject(new Error('无法连接文件存储服务，请稍后重试'));
    xhr.ontimeout = () => reject(new Error('上传超时'));
    xhr.onabort = () => reject(new DOMException('Aborted', 'AbortError'));
    if (signal.aborted) xhr.abort();
    signal.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(body);
  });
}

async function uploadParts(
  localId: string,
  init: InitResponse,
  payload: PreparedPayload,
  signal: AbortSignal,
): Promise<void> {
  const partSize = init.partSize!;
  const parts = init.parts!;
  const total = payload.source.size;
  const partLoaded = new Map<number, number>();
  const emit = () => {
    let loaded = 0;
    for (const value of partLoaded.values()) loaded += value;
    post({
      type: 'progress',
      localId,
      phase: 'uploading',
      progress: Math.min(0.999, loaded / total),
    });
  };

  const settled = await mapWithConcurrency(
    parts,
    UPLOAD_CONCURRENCY,
    async (part) => {
      const start = (part.number - 1) * partSize;
      const end = Math.min(start + partSize, total);
      const blob = payload.source.slice(start, end);
      const etag = await withRetry(
        () =>
          putViaXhr(part.uploadUrl, blob, {}, signal, (loaded) => {
            partLoaded.set(part.number, loaded);
            emit();
          }),
        PART_MAX_RETRIES,
      );
      return { number: part.number, etag };
    },
  );

  const completed: Array<{ number: number; etag: string }> = [];
  for (const result of settled) {
    if (result.status === 'fulfilled') completed.push(result.value);
    else throw new Error('部分分片上传失败，请检查网络后重试');
  }
  if (completed.length !== partCountForSize(total, partSize)) {
    throw new Error('分片数量不完整，请重试');
  }
  init.completedParts = completed;
}

// ── 5. 本地解析（PDF / Word / 文本，失败静默）────────────────────────

const TEXT_PREVIEW_LIMIT = 6000;

function detectLocalParseKind(
  file: File,
): LocalDocumentPreview['kind'] | null {
  const ext = file.name.includes('.')
    ? file.name.split('.').pop()!.toLowerCase()
    : '';
  if (file.type === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (
    file.type ===
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document' ||
    ext === 'docx'
  ) {
    return 'docx';
  }
  if (
    file.type.startsWith('text/') ||
    (ext !== 'svg' && isGzipCompressible(file.name, file.type))
  ) {
    return 'text';
  }
  return null;
}

async function runLocalParse(
  file: File,
  kind: LocalDocumentPreview['kind'],
): Promise<LocalDocumentPreview> {
  if (kind === 'pdf') {
    const data = await file.arrayBuffer();
    const params: DocumentInitParameters = { data };
    const doc = await pdfjs.getDocument(params).promise;
    const pages = doc.numPages;
    let excerpt = '';
    try {
      const maxPages = Math.min(3, pages);
      for (let pageNumber = 1; pageNumber <= maxPages; pageNumber += 1) {
        const page = await doc.getPage(pageNumber);
        const content = await page.getTextContent();
        excerpt +=
          content.items
            .map((item) => ('str' in item ? item.str : ''))
            .join(' ') + '\n';
        if (excerpt.length >= TEXT_PREVIEW_LIMIT) break;
      }
    } finally {
      await doc.destroy();
    }
    return { kind: 'pdf', pages, excerpt: excerpt.trim().slice(0, TEXT_PREVIEW_LIMIT) };
  }
  if (kind === 'docx') {
    const result = await mammoth.extractRawText({
      arrayBuffer: await file.arrayBuffer(),
    });
    return {
      kind: 'docx',
      excerpt: String(result.value).slice(0, TEXT_PREVIEW_LIMIT),
    };
  }
  const text = new TextDecoder('utf-8').decode(await file.arrayBuffer());
  return { kind: 'text', excerpt: text.slice(0, TEXT_PREVIEW_LIMIT) };
}

// ── 编排 ─────────────────────────────────────────────────────────────

const jobs = new Map<string, AbortController>();

self.onmessage = (event: MessageEvent<WorkerInboundMessage>) => {
  const message = event.data;
  if (message.type === 'cancel') {
    jobs.get(message.localId)?.abort();
    return;
  }
  const controller = new AbortController();
  jobs.set(message.localId, controller);
  void runUpload(
    message.localId,
    message.file,
    message.options,
    message.target,
    message.contentType,
    controller.signal,
  );
};

async function runUpload(
  localId: string,
  inputFile: File,
  options: UploadOptions,
  target: UploadTarget,
  contentTypeOverride: string | undefined,
  signal: AbortSignal,
): Promise<void> {
  try {
    post({ type: 'progress', localId, phase: 'hashing', progress: 0 });
    const recompression = await maybeRecompressImage(inputFile, options, signal);
    const file = recompression.file;
    const recompressed = recompression.changed;
    const contentType =
      contentTypeOverride || file.type || 'application/octet-stream';

    const payload = await preparePayload(file, options, signal, (progress) => {
      post({
        type: 'progress',
        localId,
        phase: 'hashing',
        progress: Math.min(1, progress),
      });
    });
    post({ type: 'progress', localId, phase: 'hashing', progress: 1 });

    // 本地解析仅服务 chat 附件预览，与网络上传并行；知识库上传不浪费 CPU。
    const parseKind =
      target.kind === 'chat' ? detectLocalParseKind(file) : null;
    const parseTask = parseKind
      ? runLocalParse(file, parseKind)
        .then((preview) => post({ type: 'parsed', localId, preview }))
        .catch(() => undefined)
      : null;

    const init = await requestInit(target, file, payload, contentType, signal);
    if (init.mode === 'instant') {
      await parseTask;
      post({
        type: 'success',
        localId,
        result: init.result,
        instant: true,
        compressed: recompressed,
      });
      return;
    }

    post({ type: 'progress', localId, phase: 'uploading', progress: 0 });
    if (init.mode === 'single') {
      await withRetry(
        () =>
          putViaXhr(
            init.uploadUrl!,
            payload.source,
            init.headers ?? {},
            signal,
            (loaded) => {
              post({
                type: 'progress',
                localId,
                phase: 'uploading',
                progress: Math.min(0.999, loaded / payload.source.size),
              });
            },
          ),
        PART_MAX_RETRIES,
      );
    } else {
      await uploadParts(localId, init, payload, signal);
    }

    post({ type: 'progress', localId, phase: 'verifying', progress: 0 });
    const result = await requestComplete(target, init, payload, signal);
    post({ type: 'progress', localId, phase: 'verifying', progress: 1 });

    await parseTask;
    post({
      type: 'success',
      localId,
      result,
      instant: false,
      compressed: recompressed || payload.contentEncoding === 'gzip',
    });
  } catch (error) {
    if (signal.aborted) return;
    post({
      type: 'error',
      localId,
      message: error instanceof Error ? error.message : '上传失败',
    });
  } finally {
    jobs.delete(localId);
  }
}
