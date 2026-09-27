'use client';

import { useEffect, useMemo, useState, type ReactNode } from 'react';

import {
  retrieveKnowledge,
  type KnowledgeBase,
  type KnowledgeCitation,
  type KnowledgeSearchResult,
} from './knowledge-api';
import { retrievalViaLabel } from './knowledge-helpers';
import { scheduleMicrotask } from '../lib/schedule-microtask';
import {
  Badge,
  CitationImages,
  DocIcon,
  EmptyState,
  HistoryIcon,
  RetrievalParamsPanel,
  type RetrievalParamsValue,
  SearchIcon,
  Spinner,
} from './knowledge-ui';

function historyStorageKey(kbId: string) {
  return `kc-retrieval-history:${kbId}`;
}

function loadHistory(kbId: string): string[] {
  try {
    const raw = localStorage.getItem(historyStorageKey(kbId));
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string').slice(0, 10) : [];
  } catch {
    return [];
  }
}

function escapeRegExp(input: string) {
  return input.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function highlight(text: string, query: string): ReactNode[] {
  const terms = Array.from(new Set(query.split(/\s+/).map((term) => term.trim()).filter(Boolean))).slice(0, 8);
  if (!terms.length) return [text];
  const pattern = new RegExp(`(${terms.map(escapeRegExp).join('|')})`, 'gi');
  return text.split(pattern).map((part, index) =>
    index % 2 === 1 ? <mark key={index}>{part}</mark> : <span key={index}>{part}</span>,
  );
}

export function RetrievalView({ kb }: { kb: KnowledgeBase }) {
  const [params, setParams] = useState<RetrievalParamsValue>({ topK: 10, minScore: 0 });
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<KnowledgeSearchResult | null>(null);
  const [searchedQuery, setSearchedQuery] = useState('');
  const [history, setHistory] = useState<string[]>([]);

  useEffect(() => {
    scheduleMicrotask(() => {
      setResult(null);
      setError(null);
      setQuery('');
      setSearchedQuery('');
      setHistory(loadHistory(kb.id));
    });
  }, [kb.id]);

  const runSearch = async (rawQuery: string) => {
    const nextQuery = rawQuery.trim();
    if (!nextQuery || loading) return;
    setLoading(true);
    setError(null);
    setSearchedQuery(nextQuery);
    try {
      const searchResult = await retrieveKnowledge(kb.id, {
        query: nextQuery,
        topK: params.topK,
        minScore: params.minScore,
      });
      setResult(searchResult);
      const nextHistory = [nextQuery, ...history.filter((item) => item !== nextQuery)].slice(0, 10);
      setHistory(nextHistory);
      try { localStorage.setItem(historyStorageKey(kb.id), JSON.stringify(nextHistory)); } catch { /* 忽略存储失败 */ }
    } catch (searchError) {
      setResult(null);
      setError(searchError instanceof Error ? searchError.message : '检索失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  const stats = result?.stats;
  const duration = useMemo(() => (typeof stats?.durationMs === 'number' ? `${stats.durationMs} ms` : null), [stats]);

  return (
    <div className="kc-split">
      <aside className="kc-split-side">
        <RetrievalParamsPanel value={params} onChange={setParams} />
      </aside>
      <section className="kc-split-main">
        <form
          className="kc-search-bar"
          onSubmit={(event) => { event.preventDefault(); void runSearch(query); }}
        >
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="输入问题或关键词，调试切片命中情况"
            aria-label="知识检索"
            autoFocus
          />
          <button type="submit" className="kc-button kc-button-primary kc-search-button" disabled={loading || !query.trim()}>
            <SearchIcon />
          </button>
        </form>

        {history.length > 0 && (
          <div className="kc-history">
            <span className="kc-history-label"><HistoryIcon /> 检索历史</span>
            {history.map((item) => (
              <button key={item} type="button" className="kc-history-chip" onClick={() => { setQuery(item); void runSearch(item); }}>
                {item}
              </button>
            ))}
          </div>
        )}

        {error && <p className="kc-alert kc-alert-error" role="alert">{error}</p>}

        {loading ? (
          <div className="kc-loading"><Spinner /> 正在检索知识库…</div>
        ) : result ? (
          result.citations.length === 0 ? (
            <EmptyState icon={<SearchIcon />} title="没有命中的切片" hint="试试降低最低相似度或更换关键词。" />
          ) : (
            <div className="retrieval-results">
              <p className="kc-result-meta">
                命中 <strong>{result.citations.length}</strong> 个切片
                {duration && <> · 耗时 {duration}</>}
                {typeof stats?.vectorHits === 'number' && <> · 向量召回 {Number(stats.vectorHits)}</>}
                {typeof stats?.graphHops === 'number' && <> · 图谱跳数 {Number(stats.graphHops)}</>}
              </p>
              {result.citations.map((citation) => (
                <RetrievalCard key={citation.chunkId} kbId={kb.id} citation={citation} query={searchedQuery} />
              ))}
            </div>
          )
        ) : (
          <EmptyState
            icon={<SearchIcon />}
            title="输入关键词开始检索调试"
            hint="相似度来自向量索引的真实打分，可通过左侧参数观察命中变化。"
          />
        )}
      </section>
    </div>
  );
}

function RetrievalCard({ kbId, citation, query }: { kbId: string; citation: KnowledgeCitation; query: string }) {
  return (
    <article className="retrieval-card">
      <header className="retrieval-card-head">
        <Badge tone="violet">相似度 {citation.score.toFixed(6)}</Badge>
        <span className="retrieval-doc" title={citation.documentName}>
          <DocIcon /> {citation.documentName}
        </span>
        <Badge tone="neutral">{retrievalViaLabel(citation.via)}</Badge>
      </header>
      <div className="retrieval-card-body">
        {citation.heading && <p className="chunk-heading">{citation.heading}</p>}
        <p className="chunk-text">{highlight(citation.passage, query)}</p>
        <CitationImages kbId={kbId} images={citation.images ?? []} />
      </div>
      <footer className="retrieval-card-foot">
        <span className="chunk-id" title={citation.chunkId}>{citation.chunkId}</span>
        <span>切片 #{citation.ordinal + 1}</span>
      </footer>
    </article>
  );
}
