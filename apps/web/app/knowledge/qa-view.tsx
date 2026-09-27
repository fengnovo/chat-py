'use client';

import { useEffect, useRef, useState, type KeyboardEvent } from 'react';

import {
  askKnowledge,
  type KnowledgeBase,
  type KnowledgeCitation,
} from './knowledge-api';
import { retrievalViaLabel } from './knowledge-helpers';
import {
  Badge,
  ChatIcon,
  CitationImages,
  DocIcon,
  QuoteIcon,
  RetrievalParamsPanel,
  type RetrievalParamsValue,
  SendIcon,
  Spinner,
} from './knowledge-ui';

type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  citations?: KnowledgeCitation[];
  pending?: boolean;
  error?: boolean;
};

const GREETING: ChatMessage = {
  id: 'greeting',
  role: 'assistant',
  content: 'Hi，我是知识问答助手。选择左侧知识库后，直接向我提问，我会基于知识库内容给出带来源的回答。',
};

const MAX_QUESTION_LENGTH = 8_000;

export function QaView({ kb }: { kb: KnowledgeBase }) {
  const [params, setParams] = useState<RetrievalParamsValue>({ topK: 10, minScore: 0 });
  const [messages, setMessages] = useState<ChatMessage[]>([GREETING]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setMessages([{ ...GREETING, content: `Hi，我是知识问答助手。当前知识库为「${kb.name}」，直接向我提问吧。` }]);
    setDraft('');
  }, [kb.id, kb.name]);

  useEffect(() => {
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [messages]);

  const citedMessages = messages.filter((message) => message.role === 'assistant' && message.citations?.length);
  const latestCitations = citedMessages.length > 0 ? citedMessages[citedMessages.length - 1]!.citations ?? [] : [];

  const send = async () => {
    const question = draft.trim();
    if (!question || sending) return;
    const userMessage: ChatMessage = { id: `u-${Date.now()}`, role: 'user', content: question };
    const pendingId = `a-${Date.now()}`;
    setMessages((current) => [...current, userMessage, { id: pendingId, role: 'assistant', content: '', pending: true }]);
    setDraft('');
    setSending(true);
    try {
      const result = await askKnowledge(kb.id, { question, topK: params.topK, minScore: params.minScore });
      setMessages((current) => current.map((message) =>
        message.id === pendingId
          ? { id: pendingId, role: 'assistant', content: result.answer, citations: result.retrieval.citations }
          : message,
      ));
    } catch (error) {
      setMessages((current) => current.map((message) =>
        message.id === pendingId
          ? { id: pendingId, role: 'assistant', content: error instanceof Error ? `回答失败：${error.message}` : '回答失败，请稍后重试。', error: true }
          : message,
      ));
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  };

  return (
    <div className="qa-layout">
      <aside className="qa-side qa-side-left">
        <RetrievalParamsPanel value={params} onChange={setParams} />
        <section className="kc-panel qa-service">
          <header className="kc-panel-head">
            <QuoteIcon className="kc-panel-icon" />
            <div>
              <h3>服务调用</h3>
              <p>发布问答参数并接入业务系统</p>
            </div>
          </header>
          <div className="qa-service-actions">
            <button type="button" className="kc-button kc-button-primary" disabled title="当前部署暂未开放">
              {'{ }'} 创建服务调用
            </button>
            <button type="button" className="kc-button" disabled title="当前部署暂未开放">
              API 调用
            </button>
          </div>
          <p className="kc-panel-hint">独立 API Key 服务暂未开放，当前可直接在页面内验证问答效果。</p>
        </section>
      </aside>

      <section className="qa-chat">
        <div className="qa-messages" ref={scrollRef}>
          {messages.map((message) => (
            <div key={message.id} className={`qa-message qa-message-${message.role}`}>
              {message.role === 'assistant' && (
                <span className="qa-avatar"><ChatIcon /></span>
              )}
              <div className={`qa-bubble${message.error ? ' qa-bubble-error' : ''}`}>
                {message.pending ? (
                  <span className="qa-pending"><Spinner /> 正在检索知识库并生成回答…</span>
                ) : (
                  <span className="qa-content">{message.content}</span>
                )}
              </div>
            </div>
          ))}
        </div>
        <div className="qa-composer">
          <textarea
            value={draft}
            maxLength={MAX_QUESTION_LENGTH}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="基于当前知识库提问，Enter 发送，Shift+Enter 换行"
            aria-label="输入问题"
          />
          <div className="qa-composer-bar">
            <span className="qa-counter">{draft.length}/{MAX_QUESTION_LENGTH}</span>
            <button type="button" className="kc-button kc-button-primary qa-send" onClick={() => void send()} disabled={sending || !draft.trim()}>
              <SendIcon /> 发送
            </button>
          </div>
        </div>
      </section>

      <aside className="qa-side qa-side-right">
        <section className="kc-panel qa-citations">
          <header className="kc-panel-head">
            <QuoteIcon className="kc-panel-icon" />
            <div>
              <h3>引用来源</h3>
              <p>回答使用的命中切片固定展示在右侧</p>
            </div>
          </header>
          {latestCitations.length === 0 ? (
            <p className="kc-panel-hint">发送问题后，这里会展示引用切片。</p>
          ) : (
            <ol className="qa-citation-list">
              {latestCitations.map((citation, index) => (
                <li key={citation.chunkId} className="qa-citation">
                  <header>
                    <span className="qa-citation-index">[{index + 1}]</span>
                    <span className="retrieval-doc" title={citation.documentName}>
                      <DocIcon /> {citation.documentName}
                    </span>
                  </header>
                  <p>{citation.passage}</p>
                  <CitationImages kbId={kb.id} images={citation.images ?? []} />
                  <footer>
                    <Badge tone="violet">{citation.score.toFixed(4)}</Badge>
                    <Badge tone="neutral">{retrievalViaLabel(citation.via)}</Badge>
                  </footer>
                </li>
              ))}
            </ol>
          )}
        </section>
      </aside>
    </div>
  );
}
