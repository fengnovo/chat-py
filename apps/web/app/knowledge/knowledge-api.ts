import { apiFetch } from '../components/resilient-chat/api';
import { uploadFileWithWorker } from '../components/resilient-chat/upload-worker-client';
import { detectDocumentMime } from './knowledge-helpers';

export type KnowledgeVisibility = 'private' | 'tenant';

export type KnowledgeBase = {
  id: string;
  name: string;
  description: string;
  visibility: KnowledgeVisibility;
  status: string;
  ownerUserId: string;
  documentCount: number;
  chunkCount: number;
  graphEnabled: boolean;
  chunkSize: number;
  chunkOverlap: number;
  topK: number;
  createdAt: string;
  updatedAt: string;
};

export type KnowledgeDocument = {
  id: string;
  kbId: string;
  name: string;
  mime: string;
  status: string;
  sizeBytes: number;
  chunkCount: number;
  errorMessage: string | null;
  indexedAt: string | null;
  directory: string;
  createdAt: string;
  updatedAt: string;
};

export type KnowledgeChunk = {
  id: string;
  documentId: string;
  ordinal: number;
  text: string;
  tokenCount: number;
  heading: string | null;
  metadata?: { headingPath?: string[]; imageRefs?: Array<{ path: string; alt?: string }> };
  createdAt: string;
  documentName: string;
};

export type KnowledgeCitationImage = {
  assetId: string;
  name: string;
  mime: string;
  alt: string;
  relPath: string;
};

export type KnowledgeCitation = {
  chunkId: string;
  documentId: string;
  documentName: string;
  ordinal: number;
  heading?: string;
  score: number;
  via: string;
  passage: string;
  images?: KnowledgeCitationImage[];
};

export type KnowledgeAsset = {
  id: string;
  kbId: string;
  documentId: string | null;
  relPath: string;
  name: string;
  mime: string;
  sizeBytes: number;
  caption: string | null;
  createdAt: string;
};

export type KnowledgeSearchResult = {
  retrievalId: string;
  citations: KnowledgeCitation[];
  relations: Array<{ source: string; relation: string; target: string; chunkIds: string[] }>;
  stats: { vectorHits?: number; graphHops?: number; durationMs?: number;[key: string]: unknown };
};

// 目录导入时前端会在短时间内发出大量请求，被限流的请求按 Retry-After 退避后自动重放。
const RATE_LIMIT_RETRY_ATTEMPTS = 4;
const RATE_LIMIT_MAX_WAIT_MS = 15_000;

function rateLimitWaitMs(response: Response, attempt: number): number {
  const seconds = Number(response.headers.get('retry-after'));
  const suggested = Number.isFinite(seconds) && seconds > 0 ? seconds * 1_000 : 0;
  return Math.min(Math.max(suggested, 2 ** attempt * 500), RATE_LIMIT_MAX_WAIT_MS);
}

async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  let response = await apiFetch(input, init);
  // body 在本模块内始终是字符串，可直接复用同一 init 重放。
  for (let attempt = 1; response.status === 429 && attempt < RATE_LIMIT_RETRY_ATTEMPTS; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, rateLimitWaitMs(response, attempt - 1)));
    response = await apiFetch(input, init);
  }
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { error?: string; message?: string } | null;
    throw new Error(payload?.message || payload?.error || `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function normalizeBase(row: Record<string, unknown>): KnowledgeBase {
  return {
    id: String(row.id),
    name: String(row.name ?? ''),
    description: String(row.description ?? ''),
    visibility: (row.visibility === 'tenant' ? 'tenant' : 'private') as KnowledgeVisibility,
    status: String(row.status ?? 'ready'),
    ownerUserId: String(row.owner_user_id ?? ''),
    documentCount: Number(row.document_count ?? 0),
    chunkCount: Number(row.chunk_count ?? 0),
    graphEnabled: Boolean(row.graph_enabled ?? true),
    chunkSize: Number(row.chunk_size ?? 800),
    chunkOverlap: Number(row.chunk_overlap ?? 100),
    topK: Number(row.top_k ?? 10),
    createdAt: String(row.created_at ?? ''),
    updatedAt: String(row.updated_at ?? row.created_at ?? ''),
  };
}

function normalizeDocument(row: Record<string, unknown>): KnowledgeDocument {
  return {
    id: String(row.id),
    kbId: String(row.kb_id ?? ''),
    name: String(row.name ?? ''),
    mime: String(row.mime ?? ''),
    status: String(row.status ?? 'pending'),
    sizeBytes: Number(row.size_bytes ?? 0),
    chunkCount: Number(row.chunk_count ?? 0),
    errorMessage: (row.error_message as string | null) ?? null,
    indexedAt: (row.indexed_at as string | null) ?? null,
    directory: String(row.directory ?? ''),
    createdAt: String(row.created_at ?? ''),
    updatedAt: String(row.updated_at ?? row.created_at ?? ''),
  };
}

function normalizeAsset(row: Record<string, unknown>): KnowledgeAsset {
  return {
    id: String(row.id),
    kbId: String(row.kb_id ?? ''),
    documentId: (row.document_id as string | null) ?? null,
    relPath: String(row.rel_path ?? ''),
    name: String(row.name ?? ''),
    mime: String(row.mime ?? ''),
    sizeBytes: Number(row.size_bytes ?? 0),
    caption: (row.caption as string | null) ?? null,
    createdAt: String(row.created_at ?? ''),
  };
}

function normalizeChunk(row: Record<string, unknown>): KnowledgeChunk {
  return {
    id: String(row.id),
    documentId: String(row.document_id),
    ordinal: Number(row.ordinal ?? 0),
    text: String(row.text ?? ''),
    tokenCount: Number(row.token_count ?? 0),
    heading: (row.heading as string | null) ?? null,
    metadata: (row.metadata as KnowledgeChunk['metadata']) ?? undefined,
    createdAt: String(row.created_at ?? ''),
    documentName: String(row.document_name ?? ''),
  };
}

export async function listKnowledgeBases(signal?: AbortSignal): Promise<KnowledgeBase[]> {
  const payload = await requestJson<{ data: Array<Record<string, unknown>> }>('/api/knowledge-bases', { signal });
  return payload.data.map(normalizeBase);
}

export async function createKnowledgeBase(input: {
  name: string;
  description?: string;
  visibility?: KnowledgeVisibility;
}): Promise<KnowledgeBase> {
  const row = await requestJson<Record<string, unknown>>('/api/knowledge-bases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return normalizeBase(row);
}

export async function updateKnowledgeBase(
  kbId: string,
  input: { name?: string; description?: string | null; visibility?: KnowledgeVisibility },
): Promise<KnowledgeBase> {
  const row = await requestJson<Record<string, unknown>>(`/api/knowledge-bases/${kbId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return normalizeBase(row);
}

export async function deleteKnowledgeBase(kbId: string): Promise<void> {
  await requestJson<void>(`/api/knowledge-bases/${kbId}`, { method: 'DELETE' });
}

export async function listKnowledgeDocuments(kbId: string, signal?: AbortSignal): Promise<KnowledgeDocument[]> {
  const payload = await requestJson<{ data: Array<Record<string, unknown>> }>(
    `/api/knowledge-bases/${kbId}/documents`,
    { signal },
  );
  return payload.data.map(normalizeDocument);
}

export async function renameKnowledgeDocument(kbId: string, documentId: string, name: string): Promise<KnowledgeDocument> {
  const row = await requestJson<Record<string, unknown>>(
    `/api/knowledge-bases/${kbId}/documents/${documentId}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    },
  );
  return normalizeDocument(row);
}

export async function deleteKnowledgeDocument(kbId: string, documentId: string): Promise<void> {
  await requestJson<void>(`/api/knowledge-bases/${kbId}/documents/${documentId}`, { method: 'DELETE' });
}

export async function listDocumentChunks(
  kbId: string,
  documentId: string,
  options: { search?: string; signal?: AbortSignal } = {},
): Promise<{ chunks: KnowledgeChunk[]; total: number }> {
  const query = new URLSearchParams();
  if (options.search?.trim()) query.set('q', options.search.trim());
  const payload = await requestJson<{ data: Array<Record<string, unknown>>; total: number }>(
    `/api/knowledge-bases/${kbId}/documents/${documentId}/chunks?${query.toString()}`,
    { signal: options.signal },
  );
  return { chunks: payload.data.map(normalizeChunk), total: payload.total };
}

/** Wasm 哈希 + 秒传去重 + 大文件分片直传，与 chat 附件共用同一条上传管线。 */
export async function uploadKnowledgeDocument(kbId: string, file: File, options: { directory?: string } = {}): Promise<KnowledgeDocument> {
  return uploadKnowledgeEntity(kbId, file, { directory: options.directory, kind: 'document' }) as Promise<KnowledgeDocument>;
}

/** 通用上传工具：document / asset 共用同一套 init + confirm 流程。 */
async function uploadKnowledgeEntity(
  kbId: string,
  file: File,
  options: { kind: 'document' | 'asset'; directory?: string; relPath?: string },
): Promise<KnowledgeDocument | KnowledgeAsset> {
  // 浏览器对 .md 等扩展名常给空 MIME，按后端契约先修正再交给 Worker。
  const mime = detectDocumentMime(file.name, file.type);
  const { result } = await uploadFileWithWorker({
    file,
    target:
      options.kind === 'document'
        ? { kind: 'knowledge-document', kbId, ...(options.directory ? { directory: options.directory } : {}) }
        : { kind: 'knowledge-asset', kbId, relPath: options.relPath ?? file.name },
    options: { gzip: true, recompressImage: false },
    contentType: mime,
  });
  return options.kind === 'asset'
    ? normalizeAsset(result as Record<string, unknown>)
    : normalizeDocument(result as Record<string, unknown>);
}

export async function uploadKnowledgeAsset(
  kbId: string,
  file: File,
  options: { relPath: string } = { relPath: file.name },
): Promise<KnowledgeAsset> {
  return uploadKnowledgeEntity(kbId, file, { kind: 'asset', relPath: options.relPath }) as Promise<KnowledgeAsset>;
}

export async function listKnowledgeAssets(
  kbId: string,
  options: { documentId?: string } = {},
): Promise<KnowledgeAsset[]> {
  const query = new URLSearchParams();
  if (options.documentId) query.set('documentId', options.documentId);
  const payload = await requestJson<{ data: Array<Record<string, unknown>> }>(
    `/api/knowledge-bases/${kbId}/assets?${query.toString()}`,
  );
  return payload.data.map(normalizeAsset);
}

export async function deleteKnowledgeAsset(kbId: string, assetId: string): Promise<void> {
  await requestJson<void>(`/api/knowledge-bases/${kbId}/assets/${assetId}`, { method: 'DELETE' });
}

/** 走 API 代理读取图片二进制，避免预签名 URL 过期 + CORS 问题。 */
export function getKnowledgeAssetContentUrl(kbId: string, assetId: string): string {
  return `/api/knowledge-bases/${kbId}/assets/${assetId}/content`;
}

export async function retrieveKnowledge(
  kbId: string,
  input: { query: string; topK: number; minScore: number },
): Promise<KnowledgeSearchResult> {
  return requestJson<KnowledgeSearchResult>(`/api/knowledge-bases/${kbId}/retrieval`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
}

export async function askKnowledge(
  kbId: string,
  input: { question: string; topK: number; minScore: number },
): Promise<{ answer: string; retrieval: KnowledgeSearchResult }> {
  return requestJson<{ answer: string; retrieval: KnowledgeSearchResult }>(
    `/api/knowledge-bases/${kbId}/ask`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    },
  );
}
