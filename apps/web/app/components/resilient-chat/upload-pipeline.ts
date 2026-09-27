import type { ChatAttachmentView } from './attachments-api';

// ── 上传参数（与后端路由阈值对齐）────────────────────────────────────
/** 单片大小：5MiB，MinIO/S3 允许的最小分片。 */
export const PART_SIZE = 5 * 1024 * 1024;
/** 超过该大小走分片直传。 */
export const MULTIPART_THRESHOLD = 8 * 1024 * 1024;
/** 分片并发数：弱网下并发过高压垮请求，3 为业界常用稳妥值。 */
export const UPLOAD_CONCURRENCY = 3;
/** 单片失败重试次数。 */
export const PART_MAX_RETRIES = 2;

/** 适合 gzip 的文本类扩展名（PDF/DOCX/图片本身已是压缩格式，不重复压缩）。 */
const GZIP_EXTENSIONS = new Set([
  'txt', 'md', 'markdown', 'json', 'csv', 'log', 'yaml', 'yml',
  'xml', 'html', 'htm', 'css', 'js', 'mjs', 'cjs', 'ts', 'tsx',
  'jsx', 'py', 'sh', 'sql', 'ini', 'conf', 'toml', 'svg',
]);

export type UploadPhase = 'hashing' | 'uploading' | 'verifying';

/** 本地解析文档得到的预览信息。 */
export interface LocalDocumentPreview {
  kind: 'pdf' | 'docx' | 'text';
  /** 提取的纯文本（已截断），供发送前预览。 */
  excerpt: string;
  /** PDF 页数。 */
  pages?: number;
}

export interface UploadOptions {
  /** 对文本类文件做 gzip 压缩。 */
  gzip: boolean;
  /** 粘贴图片允许 canvas 重编码压缩（截图 PNG 通常可缩小数倍）。 */
  recompressImage: boolean;
}

/** 上传目的地：chat 附件与知识库文档/资源共用同一条 Wasm 哈希 + 压缩 + 分片管线。 */
export type UploadTarget =
  | { kind: 'chat' }
  | { kind: 'knowledge-document'; kbId: string; directory?: string }
  | { kind: 'knowledge-asset'; kbId: string; relPath?: string };

// ── Worker 通信协议 ──────────────────────────────────────────────────

export type WorkerInboundMessage =
  | {
      type: 'upload';
      localId: string;
      file: File;
      options: UploadOptions;
      target: UploadTarget;
      /** 主线程修正后的 MIME（浏览器对 .md 等常给空串）。 */
      contentType?: string;
    }
  | { type: 'cancel'; localId: string };

export type WorkerOutboundMessage =
  | {
      type: 'progress';
      localId: string;
      phase: UploadPhase;
      /** 0~1。 */
      progress: number;
    }
  | { type: 'parsed'; localId: string; preview: LocalDocumentPreview }
  | {
      type: 'success';
      localId: string;
      /** 目的地返回的领域对象：ChatAttachmentView / KnowledgeDocument / KnowledgeAsset。 */
      result: unknown;
      /** 命中秒传。 */
      instant: boolean;
      /** 经客户端压缩（gzip 或图片重编码）。 */
      compressed: boolean;
    }
  | { type: 'error'; localId: string; message: string };

/** 判定文件是否适合 gzip：仅文本/代码/SVG 等高压缩比类型。 */
export function isGzipCompressible(
  filename: string,
  contentType: string,
): boolean {
  if (contentType.startsWith('text/')) return true;
  const extension = filename.includes('.')
    ? filename.split('.').pop()!.toLowerCase()
    : '';
  return GZIP_EXTENSIONS.has(extension);
}

/** 按存储字节数计算分片数（至少 1 片）。 */
export function partCountForSize(
  storedSizeBytes: number,
  partSize: number = PART_SIZE,
): number {
  if (storedSizeBytes <= 0) return 1;
  return Math.max(1, Math.ceil(storedSizeBytes / partSize));
}

/**
 * 受控并发执行：按 limit 分批，每批全部结束后再开下一批，
 * 以 allSettled 结果返回——单任务失败不影响同批其他任务。
 */
export async function mapWithConcurrency<T, R>(
  items: T[],
  limit: number,
  worker: (item: T, index: number) => Promise<R>,
): Promise<Array<PromiseSettledResult<R>>> {
  const results: Array<PromiseSettledResult<R>> = [];
  const batchSize = Math.max(1, limit);
  for (let start = 0; start < items.length; start += batchSize) {
    const batch = items.slice(start, start + batchSize);
    const settled = await Promise.allSettled(
      batch.map((item, offset) => worker(item, start + offset)),
    );
    results.push(...settled);
  }
  return results;
}

/** 失败自动重试；AbortError（用户取消）不重试，直接抛出。 */
export async function withRetry<T>(
  task: (attempt: number) => Promise<T>,
  maxRetries: number = PART_MAX_RETRIES,
): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    try {
      return await task(attempt);
    } catch (error) {
      if ((error as { name?: string })?.name === 'AbortError') throw error;
      lastError = error;
    }
  }
  throw lastError instanceof Error ? lastError : new Error('请求失败');
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

/** 剪贴板图片默认名：clipboard-20260927-153012.png。 */
export function clipboardImageFileName(date: Date = new Date()): string {
  const stamp =
    `${date.getFullYear()}${pad2(date.getMonth() + 1)}${pad2(date.getDate())}` +
    `-${pad2(date.getHours())}${pad2(date.getMinutes())}${pad2(date.getSeconds())}`;
  return `clipboard-${stamp}.png`;
}

/**
 * 从粘贴事件的 DataTransfer 中提取图片文件。
 * 没有图片时返回空数组，由调用方保持默认粘贴行为（粘贴文本）。
 */
export function imageFilesFromClipboard(
  dataTransfer: DataTransfer | null,
): File[] {
  if (!dataTransfer) return [];
  const files: File[] = [];
  for (const item of Array.from(dataTransfer.items)) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  return files;
}

// ── init / complete 协议（Worker 内执行；纯 fetch，可单测）────────────

/** preparePayload 的产物：原始哈希 + 实际存储字节与哈希。 */
export interface PreparedPayload {
  source: Blob;
  contentSha256: string;
  storedSha256: string;
  contentEncoding?: 'gzip';
}

export interface InitPartPlan {
  number: number;
  uploadUrl: string;
}

export interface InitResponse {
  mode: 'instant' | 'single' | 'multipart';
  /** 领域对象 id（complete 路径用）。 */
  id: string;
  /** 目的地返回的领域对象。 */
  result: unknown;
  uploadUrl?: string;
  headers?: Record<string, string>;
  partSize?: number;
  parts?: InitPartPlan[];
  /** 分片模式下客户端暂存的完成分片，complete 时回传。 */
  completedParts?: Array<{ number: number; etag: string }>;
}

const INIT_ERROR_MESSAGES: Record<string, string> = {
  image_attachment_too_large: '图片不能超过 10MB',
  text_attachment_too_large: '文本文件不能超过 200KB',
  file_attachment_too_large: '文件不能超过 50MB',
  knowledge_document_too_large: '文件超过知识库大小限制',
  document_mime_unsupported: '知识库不支持该文档类型',
  asset_mime_unsupported: '知识库图片仅支持 PNG / JPEG / WebP / GIF',
  asset_too_small: '图片文件过小，可能不是有效图片',
};

async function readErrorMessage(response: Response, fallback: string): Promise<string> {
  const data = (await response.json().catch(() => null)) as
    | { error?: string; message?: string }
    | null;
  return (data?.error && INIT_ERROR_MESSAGES[data.error]) || data?.message || fallback;
}

/** 上传初始化：按目标分流到 chat 附件或知识库 uploads 接口。 */
export async function requestInit(
  target: UploadTarget,
  file: File,
  payload: PreparedPayload,
  contentType: string,
  signal: AbortSignal,
): Promise<InitResponse> {
  const encoding = payload.contentEncoding
    ? { contentEncoding: payload.contentEncoding }
    : {};
  if (target.kind === 'chat') {
    const response = await fetch('/api/agent/chat-attachments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify({
        filename: file.name,
        contentType,
        sizeBytes: file.size,
        contentSha256: payload.contentSha256,
        storedSha256: payload.storedSha256,
        storedSizeBytes: payload.source.size,
        ...encoding,
      }),
    });
    if (!response.ok) throw new Error(await readErrorMessage(response, '上传初始化失败'));
    const data = (await response.json()) as {
      mode: InitResponse['mode'];
      attachment: ChatAttachmentView;
      uploadUrl?: string;
      headers?: Record<string, string>;
      partSize?: number;
      parts?: InitPartPlan[];
    };
    return { ...data, id: data.attachment.id, result: data.attachment };
  }

  const knowledgeBody: Record<string, unknown> = {
    name: file.name,
    mime: contentType,
    sizeBytes: file.size,
    sha256: payload.contentSha256,
    storedSha256: payload.storedSha256,
    storedSizeBytes: payload.source.size,
    ...encoding,
  };
  if (target.kind === 'knowledge-document') {
    knowledgeBody.kind = 'document';
    if (target.directory) knowledgeBody.directory = target.directory;
  } else {
    knowledgeBody.kind = 'asset';
    knowledgeBody.relPath = target.relPath ?? file.name;
  }
  const response = await fetch(
    `/api/knowledge-bases/${target.kbId}/documents/uploads`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify(knowledgeBody),
    },
  );
  if (!response.ok) throw new Error(await readErrorMessage(response, '上传初始化失败'));
  const data = (await response.json()) as {
    mode: InitResponse['mode'];
    document?: { id: string };
    asset?: { id: string };
    upload?: { uploadUrl?: string; url?: string; headers?: Record<string, string> };
    partSize?: number;
    parts?: InitPartPlan[];
  };
  const entity = data.document ?? data.asset;
  if (!entity) throw new Error('上传初始化响应缺少文档信息');
  return {
    mode: data.mode,
    id: entity.id,
    result: entity,
    uploadUrl: data.upload?.uploadUrl ?? data.upload?.url,
    headers: data.upload?.headers,
    partSize: data.partSize,
    parts: data.parts,
  };
}

/** 上传完成：服务端校验对象（+分片合并），返回最终领域对象。 */
export async function requestComplete(
  target: UploadTarget,
  init: InitResponse,
  payload: PreparedPayload,
  signal: AbortSignal,
): Promise<unknown> {
  const multipart = init.mode === 'multipart';
  if (multipart && !init.completedParts?.length) {
    throw new Error('分片数量不完整，请重试');
  }
  if (target.kind === 'chat') {
    const response = await fetch(
      `/api/agent/chat-attachments/${init.id}/complete`,
      {
        method: 'POST',
        signal,
        ...(multipart
          ? {
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ parts: init.completedParts }),
            }
          : {}),
      },
    );
    if (!response.ok) throw new Error(await readErrorMessage(response, '文件校验失败'));
    const data = (await response.json()) as { attachment: unknown };
    return data.attachment;
  }

  const confirmPath =
    target.kind === 'knowledge-document'
      ? `/api/knowledge-bases/${target.kbId}/documents/${init.id}/confirm`
      : `/api/knowledge-bases/${target.kbId}/assets/${init.id}/confirm`;
  const response = await fetch(confirmPath, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify({
      sizeBytes: payload.source.size,
      sha256: payload.storedSha256,
      ...(multipart ? { parts: init.completedParts } : {}),
    }),
  });
  if (!response.ok) throw new Error(await readErrorMessage(response, '文件校验失败'));
  const data = (await response.json()) as { document?: unknown; asset?: unknown };
  return data.document ?? data.asset ?? data;
}
