'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { Icon } from '../components/resilient-chat/icon';
import { Lightbox, type LightboxImage } from '../components/resilient-chat/lightbox';
import { MarkdownContent } from '../components/resilient-chat/message';
import { getKnowledgeAssetContentUrl } from '../knowledge/knowledge-api';
import { streamRagAnswer, fetchKnowledgeBases } from './rag-api';
import type { KnowledgeBase, RagCitation, RagMessage, RagStep } from './types';

function generateId() {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export interface CustomerServiceChatProps {
  className?: string;
  onClose?: () => void;
}

function StepIcon({ step, status }: { step: RagStep['step']; status: RagStep['status'] }) {
  const isDone = status === 'completed';
  const isError = status === 'error';
  const iconClass = isError ? 'cs-step-icon-error' : isDone ? 'cs-step-icon-done' : 'cs-step-icon-running';
  let symbol = '●';
  if (step === 'query-analysis') symbol = '🔍';
  else if (step === 'query-expansion') symbol = '✏️';
  else if (step === 'retrieval') symbol = '📚';
  else if (step === 'source-ranking') symbol = '📊';
  else if (step === 'answer-generation') symbol = '✨';
  return <span className={`cs-step-icon ${iconClass}`}>{isDone ? '✓' : isError ? '!' : symbol}</span>;
}

function ThinkingCard({ steps }: { steps: RagStep[] }) {
  if (steps.length === 0) return null;
  return (
    <div className="cs-thinking-card">
      <div className="cs-thinking-title">思考过程</div>
      <ul className="cs-thinking-list">
        {steps.map((s, i) => (
          <li key={i} className={`cs-thinking-item cs-thinking-item-${s.status}`}>
            <StepIcon step={s.step} status={s.status} />
            <div className="cs-thinking-content">
              <div className="cs-thinking-step-title">{s.title}</div>
              {s.detail ? <div className="cs-thinking-detail">{s.detail}</div> : null}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CitationList({
  citations,
  kbId,
  onPreviewImage,
}: {
  citations: RagCitation[];
  kbId: string;
  onPreviewImage: (url: string, filename?: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  if (citations.length === 0) return null;
  const imageCount = citations.reduce((sum, c) => sum + (c.images?.length ?? 0), 0);
  return (
    <div className="cs-citations">
      <button
        type="button"
        className={`cs-citations-header ${expanded ? 'is-open' : ''}`}
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="cs-citations-title">
          参考来源（{citations.length}）
          {imageCount > 0 ? <span className="cs-citations-images-badge">含 {imageCount} 张图</span> : null}
        </span>
        <span className="cs-citations-chevron">
          <Icon name="chevron" size={14} />
        </span>
      </button>
      {expanded && (
        <div className="cs-citation-list">
          {citations.map((c, i) => (
            <div key={c.chunkId} className="cs-citation-item">
              <span className="cs-citation-pill">
                <span className="cs-citation-index">[{i + 1}]</span>
                <span className="cs-citation-doc">{c.documentName}</span>
                {c.heading ? <span className="cs-citation-heading">/ {c.heading}</span> : null}
                <span className={`cs-citation-via cs-citation-via-${c.via}`}>{c.via}</span>
              </span>
              {c.images && c.images.length > 0 ? (
                <div className="cs-citation-images">
                  {c.images.map((image) => {
                    const url = getKnowledgeAssetContentUrl(kbId, image.assetId);
                    const label = image.alt || image.name;
                    return (
                      <button
                        type="button"
                        key={image.assetId}
                        className="cs-citation-image"
                        onClick={() => onPreviewImage(url, label)}
                        aria-label={`查看图片 ${label}`}
                        title={label}
                      >
                        <img src={url} alt={label} loading="lazy" />
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MessageBubble({
  message,
  kbId,
  onPreviewImage,
}: {
  message: RagMessage;
  kbId: string;
  onPreviewImage: (url: string, filename?: string) => void;
}) {
  const isUser = message.role === 'user';
  return (
    <div className={`cs-message ${isUser ? 'cs-message-user' : 'cs-message-assistant'}`}>
      <div className="cs-message-avatar">{isUser ? '👤' : '🤖'}</div>
      <div className="cs-message-body">
        {message.steps && message.steps.length > 0 ? <ThinkingCard steps={message.steps} /> : null}
        {isUser ? (
          <div className="cs-message-text">
            {message.content}
            {message.isStreaming ? <span className="cs-cursor" /> : null}
          </div>
        ) : (
          <div className="cs-message-text cs-message-markdown markdown-content">
            {message.content ? (
              <MarkdownContent
                content={message.content}
                citations={message.citations?.map((c) => ({ kbId, images: c.images }))}
                onPreviewImage={onPreviewImage}
                highlight
              />
            ) : null}
            {message.isStreaming ? <span className="cs-cursor" /> : null}
          </div>
        )}
        {message.error ? <div className="cs-message-error">{message.error}</div> : null}
        {message.citations ? (
          <CitationList citations={message.citations} kbId={kbId} onPreviewImage={onPreviewImage} />
        ) : null}
      </div>
    </div>
  );
}

export default function CustomerServiceChat({ className = 'cs-page', onClose }: CustomerServiceChatProps) {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<string>('');
  const [messages, setMessages] = useState<RagMessage[]>([
    {
      id: generateId(),
      role: 'assistant',
      content: '您好，我是您的 AI 客服助手。请选择知识库并输入问题，我会先检索相关资料再作答。',
    },
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [kbOpen, setKbOpen] = useState(false);
  const [lightboxImage, setLightboxImage] = useState<LightboxImage | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesRef = useRef<RagMessage[]>(messages);
  const kbRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    fetchKnowledgeBases()
      .then((list) => {
        setKnowledgeBases(list);
        if (list.length > 0 && !selectedKbId) setSelectedKbId(list[0].id);
      })
      .catch((err) => setError(err.message));
  }, [selectedKbId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // 知识库下拉点击外部关闭
  useEffect(() => {
    if (!kbOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (kbRef.current && !kbRef.current.contains(event.target as Node)) {
        setKbOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [kbOpen]);

  // 正文 markdown 图片与引用来源缩略图共用同一个大图浮层。
  const handlePreviewImage = useCallback((url: string, filename?: string) => {
    setLightboxImage({ url, filename });
  }, []);

  const buildHistory = useCallback((question: string) => {
    return [
      ...messagesRef.current
        .filter((m) => !m.error && m.content.trim())
        .slice(-10)
        .map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content })),
      { role: 'user' as const, content: question },
    ];
  }, []);

  const handleSend = useCallback(async () => {
    const question = input.trim();
    if (!question || isLoading || !selectedKbId) return;

    setInput('');
    setIsLoading(true);
    setError(null);

    const userMessage: RagMessage = { id: generateId(), role: 'user', content: question };
    const assistantMessage: RagMessage = {
      id: generateId(),
      role: 'assistant',
      content: '',
      steps: [],
      citations: [],
      isStreaming: true,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);

    abortRef.current = new AbortController();

    try {
      await streamRagAnswer({
        kbId: selectedKbId,
        question,
        history: buildHistory(question),
        signal: abortRef.current.signal,
        onChunk: (chunk) => {
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (!last || last.role !== 'assistant') return prev;
            const next = { ...last };
            if (chunk.type === 'step') {
              const existingIndex = next.steps?.findIndex((s) => s.step === chunk.step) ?? -1;
              if (existingIndex >= 0 && next.steps) {
                next.steps[existingIndex] = {
                  step: chunk.step,
                  status: chunk.status,
                  title: chunk.title,
                  detail: chunk.detail,
                };
              } else {
                next.steps = [...(next.steps ?? []), { step: chunk.step, status: chunk.status, title: chunk.title, detail: chunk.detail }];
              }
            } else if (chunk.type === 'citations') {
              next.citations = chunk.citations;
            } else if (chunk.type === 'delta') {
              next.content = next.content + chunk.delta;
            } else if (chunk.type === 'error') {
              next.error = chunk.message;
              next.isStreaming = false;
            } else if (chunk.type === 'done') {
              next.isStreaming = false;
            }
            return [...prev.slice(0, -1), next];
          });
        },
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === 'assistant') {
          return [...prev.slice(0, -1), { ...last, error: message, isStreaming: false }];
        }
        return prev;
      });
    } finally {
      setIsLoading(false);
      abortRef.current = null;
    }
  }, [input, isLoading, selectedKbId, buildHistory]);

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setIsLoading(false);
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === 'assistant') {
        return [...prev.slice(0, -1), { ...last, isStreaming: false }];
      }
      return prev;
    });
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        void handleSend();
      }
    },
    [handleSend],
  );

  return (
    <div className={className}>
      <header className="cs-header">
        <h1 className="cs-title">AI 客服助手</h1>
        <div className="cs-header-actions">
          <div className="cs-kb-select" ref={kbRef}>
          <span className="cs-kb-label">知识库</span>
          <button
            type="button"
            className="cs-kb-trigger"
            aria-haspopup="listbox"
            aria-expanded={kbOpen}
            disabled={isLoading}
            onClick={() => setKbOpen((open) => !open)}
          >
            <span className="cs-kb-current">
              {knowledgeBases.find((kb) => kb.id === selectedKbId)?.name ?? '请先创建知识库'}
            </span>
            <span className={`cs-kb-chevron ${kbOpen ? 'is-open' : ''}`}>
              <Icon name="chevron" size={14} />
            </span>
          </button>
          {kbOpen && (
            <div className="cs-kb-popover" role="listbox">
              {knowledgeBases.length === 0 ? (
                <div className="cs-kb-empty">请先创建知识库</div>
              ) : (
                knowledgeBases.map((kb) => {
                  const selected = kb.id === selectedKbId;
                  return (
                    <button
                      key={kb.id}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      className={selected ? 'is-selected' : ''}
                      onClick={() => {
                        setSelectedKbId(kb.id);
                        setKbOpen(false);
                      }}
                    >
                      <span className="cs-kb-check" aria-hidden="true">
                        {selected ? <Icon name="check" size={14} /> : null}
                      </span>
                      <span className="cs-kb-name">{kb.name}</span>
                    </button>
                  );
                })
              )}
            </div>
          )}
        </div>
        {onClose && (
          <button
            type="button"
            className="cs-header-close"
            aria-label="关闭 AI 客服"
            onClick={onClose}
          >
            <Icon name="x" size={16} />
          </button>
        )}
      </div>
    </header>

    <main className="cs-chat">
        {messages.map((m) => (
          <MessageBubble
            key={m.id}
            message={m}
            kbId={selectedKbId}
            onPreviewImage={handlePreviewImage}
          />
        ))}
        <div ref={messagesEndRef} />
      </main>

      {error ? <div className="cs-global-error">{error}</div> : null}

      <footer className="cs-input-area">
        <div className="cs-input-wrapper">
          <textarea
            className="cs-input"
            rows={2}
            placeholder="输入您的问题"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isLoading}
          />
          {isLoading ? (
            <button
              className="cs-send-btn is-stop"
              onClick={handleStop}
              type="button"
              aria-label="停止生成"
            >
              <Icon name="square" size={17} />
            </button>
          ) : (
            <button
              className="cs-send-btn"
              onClick={() => void handleSend()}
              disabled={!input.trim() || !selectedKbId}
              type="button"
              aria-label="发送消息"
            >
              <Icon name="arrow" size={18} />
            </button>
          )}
        </div>
        <div className="cs-footer-note">回答由 AI 生成，仅供参考，请以官方文档为准。</div>
      </footer>

      <Lightbox image={lightboxImage} onClose={() => setLightboxImage(null)} />
    </div>
  );
}
