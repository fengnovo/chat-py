import { apiFetch } from '@/app/components/resilient-chat/api';
import type { RagStreamChunk, KnowledgeBase } from './types';

export async function fetchKnowledgeBases(): Promise<KnowledgeBase[]> {
  const res = await apiFetch('/api/knowledge-bases');
  if (!res.ok) throw new Error('加载知识库失败');
  const data = (await res.json()) as { data: KnowledgeBase[] };
  return data.data ?? [];
}

export interface RagStreamOptions {
  kbId: string;
  question: string;
  topK?: number;
  minScore?: number;
  history?: Array<{ role: 'user' | 'assistant'; content: string }>;
  onChunk: (chunk: RagStreamChunk) => void;
  signal?: AbortSignal;
}

export async function streamRagAnswer(options: RagStreamOptions): Promise<void> {
  const res = await apiFetch(`/api/knowledge-bases/${encodeURIComponent(options.kbId)}/rag-stream`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      question: options.question,
      ...(options.topK !== undefined ? { topK: options.topK } : {}),
      ...(options.minScore !== undefined ? { minScore: options.minScore } : {}),
      ...(options.history && options.history.length > 0 ? { history: options.history } : {}),
    }),
    signal: options.signal,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`请求失败 ${res.status}: ${text.slice(0, 200)}`);
  }

  if (!res.body) throw new Error('响应没有 body');

  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (!options.signal?.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || !trimmed.startsWith('data: ')) continue;
        const payload = trimmed.slice(6).trim();
        if (payload === '[DONE]') continue;
        let chunk: RagStreamChunk;
        try {
          chunk = JSON.parse(payload) as RagStreamChunk;
        } catch {
          continue;
        }
        options.onChunk(chunk);
        if (chunk.type === 'done' || chunk.type === 'error') {
          return;
        }
      }
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
}
