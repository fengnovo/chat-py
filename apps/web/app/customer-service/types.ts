export interface RagStep {
  step: 'query-analysis' | 'query-expansion' | 'retrieval' | 'source-ranking' | 'answer-generation';
  status: 'running' | 'completed' | 'error';
  title: string;
  detail?: string;
}

/** 命中切片关联的知识库图片。assetId 配合 kbId 可换取代理访问地址。 */
export interface RagCitationImage {
  assetId: string;
  name: string;
  mime: string;
  alt: string;
  relPath: string;
}

export interface RagCitation {
  chunkId: string;
  documentId: string;
  documentName: string;
  ordinal: number;
  heading?: string;
  score: number;
  via: 'vector' | 'graph' | 'both' | string;
  passage: string;
  images?: RagCitationImage[];
}

export interface RagMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  steps?: RagStep[];
  citations?: RagCitation[];
  error?: string;
  isStreaming?: boolean;
}

export type RagStreamChunk =
  | { type: 'step'; step: RagStep['step']; status: RagStep['status']; title: string; detail?: string }
  | { type: 'citations'; citations: RagCitation[] }
  | { type: 'delta'; delta: string }
  | { type: 'done' }
  | { type: 'error'; code: string; message: string };

export interface KnowledgeBase {
  id: string;
  name: string;
  status?: string;
}
