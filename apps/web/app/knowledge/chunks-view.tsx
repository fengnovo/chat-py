'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  listDocumentChunks,
  listKnowledgeAssets,
  listKnowledgeDocuments,
  type KnowledgeAsset,
  type KnowledgeBase,
  type KnowledgeChunk,
  type KnowledgeDocument,
} from './knowledge-api';
import { formatDateTime } from './knowledge-helpers';
import { scheduleMicrotask } from '../lib/schedule-microtask';
import {
  ChevronDownIcon,
  CitationImages,
  EmptyState,
  GridIcon,
  LayersIcon,
  ListIcon,
  SearchIcon,
  Spinner,
} from './knowledge-ui';

export function ChunksView({
  kb,
  initialDocumentId,
}: {
  kb: KnowledgeBase;
  initialDocumentId?: string;
}) {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [documentId, setDocumentId] = useState(initialDocumentId ?? '');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([]);
  const [assets, setAssets] = useState<KnowledgeAsset[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');

  useEffect(() => {
    if (!documentId) { scheduleMicrotask(() => setAssets([])); return; }
    let cancelled = false;
    listKnowledgeAssets(kb.id, { documentId })
      .then((rows) => { if (!cancelled) setAssets(rows); })
      .catch(() => { if (!cancelled) setAssets([]); });
    return () => { cancelled = true; };
  }, [kb.id, documentId]);

  /** 同一文档内 basename 通常唯一；用于把 chunk 内 imageRefs.path 解析到 asset id 渲染缩略图。 */
  const assetsByBasename = useMemo(() => {
    const map = new Map<string, KnowledgeAsset>();
    for (const asset of assets) map.set(asset.name, asset);
    return map;
  }, [assets]);

  useEffect(() => {
    let cancelled = false;
    listKnowledgeDocuments(kb.id)
      .then((rows) => {
        if (cancelled) return;
        const ready = rows.filter((row) => row.status === 'ready' && row.chunkCount > 0);
        setDocuments(ready);
        setDocumentId((current) => {
          if (current && ready.some((row) => row.id === current)) return current;
          return initialDocumentId && ready.some((row) => row.id === initialDocumentId)
            ? initialDocumentId
            : (ready[0]?.id ?? '');
        });
      })
      .catch(() => setError('文档加载失败，请稍后重试。'));
    return () => { cancelled = true; };
  }, [kb.id, initialDocumentId]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const loadChunks = useCallback(async () => {
    if (!documentId) {
      setChunks([]);
      setTotal(0);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await listDocumentChunks(kb.id, documentId, { search: debouncedSearch });
      setChunks(result.chunks);
      setTotal(result.total);
    } catch {
      setError('切片加载失败，请稍后重试。');
    } finally {
      setLoading(false);
    }
  }, [kb.id, documentId, debouncedSearch]);

  useEffect(() => { scheduleMicrotask(() => { void loadChunks(); }); }, [loadChunks]);

  const selectedDocumentName = useMemo(
    () => documents.find((row) => row.id === documentId)?.name ?? '',
    [documents, documentId],
  );

  return (
    <div className="kc-view">
      <div className="kc-toolbar">
        <span className="kc-count">共 {total} 切片</span>
        <div className="kc-toolbar-spacer" />
        <div className="kc-select">
          <select
            value={documentId}
            onChange={(event) => setDocumentId(event.target.value)}
            aria-label="按文档筛选切片"
          >
            {documents.length === 0 && <option value="">暂无已索引文档</option>}
            {documents.map((document) => (
              <option key={document.id} value={document.id}>{document.name}</option>
            ))}
          </select>
          <ChevronDownIcon className="kc-select-arrow" />
        </div>
        <div className="kc-search">
          <SearchIcon />
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索切片 ID / 内容"
            aria-label="搜索切片"
          />
        </div>
        <div className="kc-view-toggle" role="group" aria-label="切片视图切换">
          <button
            type="button"
            className={viewMode === 'grid' ? 'is-active' : ''}
            onClick={() => setViewMode('grid')}
            aria-label="网格视图"
            aria-pressed={viewMode === 'grid'}
          >
            <GridIcon />
          </button>
          <button
            type="button"
            className={viewMode === 'list' ? 'is-active' : ''}
            onClick={() => setViewMode('list')}
            aria-label="列表视图"
            aria-pressed={viewMode === 'list'}
          >
            <ListIcon />
          </button>
        </div>
      </div>

      <p className="kc-hint">切片由文档索引管线自动生成，仅支持查看；如需调整请修改原文档后重新上传。</p>

      {error && <p className="kc-alert kc-alert-error" role="alert">{error}</p>}

      {!documentId ? (
        <EmptyState
          icon={<LayersIcon />}
          title={documents.length === 0 ? '该知识库还没有已完成索引的文档' : '请先选择一个文档'}
          hint="上传文档并等待索引完成后，这里会展示自动切分的切片。"
        />
      ) : loading ? (
        <div className="kc-loading"><Spinner /> 正在加载切片…</div>
      ) : chunks.length === 0 ? (
        <EmptyState icon={<LayersIcon />} title={debouncedSearch ? '没有匹配的切片' : '该文档暂无切片'} />
      ) : (
        <div className={viewMode === 'grid' ? 'chunk-grid' : 'chunk-list'}>
          {chunks.map((chunk) => {
            const chunkImages = (chunk.metadata?.imageRefs ?? [])
              .map((ref) => {
                const basename = (ref.path ?? '').split('/').pop() ?? '';
                const asset = assetsByBasename.get(basename);
                return asset ? { assetId: asset.id, name: asset.name, mime: asset.mime, alt: ref.alt ?? '', relPath: asset.relPath } : null;
              })
              .filter((image): image is NonNullable<typeof image> => image !== null);
            return (
              <article key={chunk.id} className="chunk-card">
                <header className="chunk-card-head">
                  <span className="chunk-order">#{chunk.ordinal + 1}</span>
                  <span className="chunk-id" title={chunk.id}>{chunk.id}</span>
                </header>
                <div className="chunk-body">
                  {chunk.heading && <p className="chunk-heading">{chunk.heading}</p>}
                  <p className="chunk-text">{chunk.text}</p>
                  <CitationImages kbId={kb.id} images={chunkImages} />
                </div>
                <footer className="chunk-foot">
                  <span title={selectedDocumentName}>{chunk.documentName}</span>
                  <span>字符 {chunk.text.length}</span>
                  <span>更新于 {formatDateTime(chunk.createdAt)}</span>
                </footer>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
