import type { KnowledgeDocument } from './knowledge-api';

export const DOCUMENT_ACCEPT = '.md,.markdown,.txt,.pdf,.docx,.xlsx,image/png,image/jpeg,image/webp,image/gif,text/markdown,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

/** 按扩展名把文件名映射到后端接受的 MIME 类型。旧版 .doc/.xls 不支持。 */
export function detectDocumentMime(name: string, fallbackType = ''): string {
  const ext = name.toLowerCase().split('.').pop();
  switch (ext) {
    case 'md':
    case 'markdown':
      return 'text/markdown';
    case 'txt':
      return 'text/plain';
    case 'pdf':
      return 'application/pdf';
    case 'docx':
      return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
    case 'xlsx':
      return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
    case 'png':
      return 'image/png';
    case 'jpg':
    case 'jpeg':
      return 'image/jpeg';
    case 'webp':
      return 'image/webp';
    case 'gif':
      return 'image/gif';
    default:
      return fallbackType || 'application/octet-stream';
  }
}

/** 是否是支持索引的文档（不含图片）。 */
export function isAcceptedKnowledgeDocument(name: string) {
  return /\.(?:md|markdown|txt|pdf|docx|xlsx)$/i.test(name);
}

/** 是否是支持上传的图片资源。 */
export function isAcceptedKnowledgeAsset(name: string) {
  return /\.(?:png|jpe?g|webp|gif)$/i.test(name);
}

/** 兼容旧名调用，新代码应按 kind 选用。 */
export function isAcceptedKnowledgeFile(name: string) {
  return isAcceptedKnowledgeDocument(name) || isAcceptedKnowledgeAsset(name);
}

export type UploadState = {
  kind: 'uploading' | 'success' | 'error';
  message: string;
};

export const DOCUMENT_LOAD_ERROR = '文档加载失败，请点击"刷新文档"重试。';

export function reserveKnowledgeUpload(active: Set<string>, kbId: string) {
  if (active.has(kbId)) return false;
  active.add(kbId);
  return true;
}

export async function finishKnowledgeUpload(
  fileName: string,
  upload: () => Promise<unknown>,
  refreshDocuments: () => Promise<unknown>,
): Promise<UploadState> {
  await upload();
  try {
    await refreshDocuments();
    return { kind: 'success', message: `${fileName} 已上传，正在等待索引。` };
  } catch {
    return {
      kind: 'error',
      message: `${fileName} 已上传，但文档列表刷新失败，请点击"刷新文档"重试。`,
    };
  }
}

export async function loadKnowledgeDocumentLists(
  bases: Array<{ id: string }>,
  fetchDocuments: (kbId: string) => Promise<KnowledgeDocument[]>,
) {
  const results = await Promise.all(
    bases.map(async (base) => {
      try {
        return { kbId: base.id, documents: await fetchDocuments(base.id) };
      } catch {
        return { kbId: base.id, error: DOCUMENT_LOAD_ERROR };
      }
    }),
  );
  const documents: Record<string, KnowledgeDocument[]> = {};
  const errors: Record<string, string> = {};
  for (const result of results) {
    if (result.documents) documents[result.kbId] = result.documents;
    if (result.error) errors[result.kbId] = result.error;
  }
  return { documents, errors };
}

export function documentStatusLabel(status: string) {
  switch (status) {
    case 'ready':
      return '处理完成';
    case 'failed':
      return '处理失败';
    case 'pending':
      return '待处理';
    case 'queued':
      return '排队中';
    case 'indexing':
    case 'processing':
      return '处理中';
    default:
      return status || '状态未知';
  }
}

export function documentStatusTone(status: string): 'green' | 'red' | 'amber' | 'neutral' {
  if (status === 'ready') return 'green';
  if (status === 'failed') return 'red';
  if (status === 'pending' || status === 'queued' || status === 'indexing' || status === 'processing') return 'amber';
  return 'neutral';
}

export function isDocumentBusy(status: string) {
  return status === 'pending' || status === 'queued' || status === 'indexing' || status === 'processing';
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function retrievalViaLabel(via: string): string {
  if (via === 'vector') return '向量召回';
  if (via === 'graph') return '图谱召回';
  if (via === 'both' || via === 'vector+graph') return '向量+图谱';
  return via;
}