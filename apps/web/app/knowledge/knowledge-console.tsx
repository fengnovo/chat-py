'use client';

import Image from 'next/image';
import Link from 'next/link';
import { useCallback, useEffect, useState, type ReactNode } from 'react';

import { listKnowledgeBases, type KnowledgeBase } from './knowledge-api';
import { useAuth } from '../components/auth/auth-context';
import { UserMenu } from '../components/auth/user-menu';
import {
  ArrowLeftIcon,
  ChatIcon,
  DatabaseIcon,
  DocIcon,
  LayersIcon,
  SearchIcon,
} from './knowledge-ui';
import { KnowledgeBaseView } from './kb-view';
import { DocumentsView } from './documents-view';
import { ChunksView } from './chunks-view';
import { RetrievalView } from './retrieval-view';
import { QaView } from './qa-view';
import { scheduleMicrotask } from '../lib/schedule-microtask';

type ViewKey = 'bases' | 'documents' | 'chunks' | 'retrieval' | 'qa';

const NAV_ITEMS: Array<{ key: ViewKey; label: string; icon: (props: { className?: string }) => ReactNode }> = [
  { key: 'bases', label: '知识库', icon: DatabaseIcon },
  { key: 'documents', label: '文档管理', icon: DocIcon },
  { key: 'chunks', label: '切片管理', icon: LayersIcon },
  { key: 'retrieval', label: '知识检索', icon: SearchIcon },
  { key: 'qa', label: '知识问答', icon: ChatIcon },
];

const STEPS = [
  { index: 1, title: '创建知识库', desc: '按业务场景组织表格知识，沉淀可检索资料', view: 'bases' as ViewKey },
  { index: 2, title: '上传文档', desc: 'Markdown / TXT 进入 Loader 与 Splitter 管线', view: 'documents' as ViewKey },
  { index: 3, title: '检索问答', desc: '调试 topK、阈值、切片命中与答案引用', view: 'retrieval' as ViewKey },
  { index: 4, title: 'API 调用', desc: '通过 SSE 接口集成到真实业务流程', view: null },
];

const SELECTED_KB_KEY = 'kc-selected-kb';

export function KnowledgeConsole() {
  const { user } = useAuth();
  const canCreate = user?.role === 'admin' || user?.role === 'owner';
  const canWriteBase = useCallback(
    (base: KnowledgeBase) => user?.role === 'admin' || (user != null && base.ownerUserId === user.id),
    [user],
  );

  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<ViewKey>('bases');
  const [selectedKbId, setSelectedKbId] = useState<string | null>(() => {
    if (typeof window === 'undefined') return null;
    try { return localStorage.getItem(SELECTED_KB_KEY); } catch { return null; }
  });
  const [initialDocumentId, setInitialDocumentId] = useState<string | undefined>(undefined);

  const refreshBases = useCallback(async () => {
    try {
      const next = await listKnowledgeBases();
      setBases(next);
      setError(null);
    } catch {
      setError('知识库加载失败，请刷新页面重试。');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { scheduleMicrotask(() => { void refreshBases(); }); }, [refreshBases]);

  const selectedKb = bases.find((base) => base.id === selectedKbId) ?? null;

  const selectKb = useCallback((base: KnowledgeBase | null) => {
    setSelectedKbId(base?.id ?? null);
    setInitialDocumentId(undefined);
    try {
      if (base) localStorage.setItem(SELECTED_KB_KEY, base.id);
      else localStorage.removeItem(SELECTED_KB_KEY);
    } catch { /* 忽略存储失败 */ }
  }, []);

  // 当前选中的知识库被删除或失去访问权限时，回到知识库列表并清理选择。
  useEffect(() => {
    if (!loading && selectedKbId && !selectedKb) {
      scheduleMicrotask(() => {
        selectKb(null);
        setView('bases');
      });
    }
  }, [loading, selectedKbId, selectedKb, selectKb]);

  const goView = (next: ViewKey) => {
    if (next !== 'bases' && !selectedKb) {
      setView('bases');
      return;
    }
    setView(next);
  };

  const openBase = (base: KnowledgeBase) => {
    selectKb(base);
    setView('documents');
  };

  const openChunks = (documentId: string) => {
    setInitialDocumentId(documentId);
    setView('chunks');
  };

  const handleChanged = async () => {
    await refreshBases();
  };

  return (
    <div className="kc-layout">
      <header className="kc-steps">
        <Link href="/" className="kc-back" title="返回首页" aria-label="返回首页">
          <ArrowLeftIcon />
          <span>返回</span>
        </Link>
        {STEPS.map((step, index) => (
          <div key={step.index} className={`kc-step${step.view ? ' kc-step-clickable' : ''}`}>
            {index > 0 && <span className="kc-step-line" aria-hidden="true" />}
            <button
              type="button"
              className="kc-step-button"
              disabled={!step.view}
              onClick={() => { if (step.view) goView(step.view); }}
            >
              <span className="kc-step-head">
                <span className="kc-step-index">第 {step.index} 步</span>
                <strong>{step.title}</strong>
              </span>
              <span className="kc-step-desc">{step.desc}</span>
            </button>
          </div>
        ))}
        <div className="kc-steps-user"><UserMenu /></div>
      </header>

      <div className="kc-body">
        <aside className="kc-sidebar">
          <div className="kc-brand">
            <span className="kc-brand-icon">
              <Image alt="Keen Agent" height={34} priority src="/keen-ai-logo.png" width={34} />
            </span>
            <strong>知识库平台</strong>
          </div>

          <nav className="kc-nav" aria-label="知识库导航">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const disabled = item.key !== 'bases' && !selectedKb;
              return (
                <button
                  key={item.key}
                  type="button"
                  className={`kc-nav-item${view === item.key ? ' is-active' : ''}`}
                  aria-current={view === item.key ? 'page' : undefined}
                  disabled={disabled}
                  title={disabled ? '请先在「知识库」中选择一个知识库' : item.label}
                  onClick={() => goView(item.key)}
                >
                  <Icon /> {item.label}
                </button>
              );
            })}
          </nav>

          <div className="kc-sidebar-bottom">
            {selectedKb ? (
              <button type="button" className="kc-current-kb" onClick={() => setView('bases')} title="返回知识库列表">
                <strong>{selectedKb.name}</strong>
                <span>{selectedKb.documentCount} 个文档</span>
                <span>{selectedKb.chunkCount} 个切片</span>
              </button>
            ) : (
              <div className="kc-current-kb kc-current-kb-empty">
                <strong>未选择知识库</strong>
                <span>在「知识库」中打开一个知识库</span>
              </div>
            )}
          </div>
        </aside>

        <main className="kc-content">
          {view === 'bases' && (
            <KnowledgeBaseView
              bases={bases}
              loading={loading}
              error={error}
              canCreate={canCreate}
              canWrite={canWriteBase}
              onChanged={handleChanged}
              onOpen={openBase}
            />
          )}
          {view !== 'bases' && selectedKb && (
            <>
              <div className="kc-content-title">
                <h2>{NAV_ITEMS.find((item) => item.key === view)?.label}</h2>
                <span className="kc-content-kb">
                  <DatabaseIcon /> {selectedKb.name}
                </span>
              </div>
              <div className="kc-content-body">
                {view === 'documents' && (
                  <DocumentsView
                    kb={selectedKb}
                    canWrite={canWriteBase(selectedKb)}
                    onViewChunks={openChunks}
                    onChanged={handleChanged}
                  />
                )}
                {view === 'chunks' && (
                  <ChunksView kb={selectedKb} initialDocumentId={initialDocumentId} />
                )}
                {view === 'retrieval' && <RetrievalView kb={selectedKb} />}
                {view === 'qa' && <QaView kb={selectedKb} />}
              </div>
            </>
          )}
          {view !== 'bases' && !selectedKb && (
            <div className="kc-need-kb">
              <p>请先选择一个知识库</p>
              <button type="button" className="kc-button kc-button-primary" onClick={() => setView('bases')}>
                前往知识库列表
              </button>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
