'use client';

import { useState, type MouseEvent } from 'react';

import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  updateKnowledgeBase,
  type KnowledgeBase,
} from './knowledge-api';
import { formatDateTime } from './knowledge-helpers';
import {
  Badge,
  DatabaseIcon,
  EditIcon,
  EmptyState,
  Modal,
  PlusIcon,
  SearchIcon,
  Spinner,
  TrashIcon,
} from './knowledge-ui';

export function KnowledgeBaseCard({
  base,
  canWrite,
  onOpen,
  onEdit,
  onDelete,
}: {
  base: KnowledgeBase;
  canWrite: boolean;
  onOpen: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const stop = (action: () => void) => (event: MouseEvent) => {
    event.stopPropagation();
    action();
  };
  return (
    <article className="kb-card" role="button" tabIndex={0} onClick={onOpen} onKeyDown={(event) => { if (event.key === 'Enter') onOpen(); }}>
      <header className="kb-card-head">
        <div className="kb-card-icon"><DatabaseIcon /></div>
        <div className="kb-card-title">
          <h3>{base.name}</h3>
          <p>{base.description || '暂无描述'}</p>
        </div>
        {canWrite && (
          <div className="kb-card-tools">
            <button type="button" className="kc-icon-button" onClick={stop(onEdit)} title="编辑知识库" aria-label={`编辑 ${base.name}`}>
              <EditIcon />
            </button>
            <button type="button" className="kc-icon-button kc-icon-danger" onClick={stop(onDelete)} title="删除知识库" aria-label={`删除 ${base.name}`}>
              <TrashIcon />
            </button>
          </div>
        )}
      </header>
      <dl className="kb-card-stats">
        <div>
          <dt>{base.graphEnabled ? 'GraphRAG' : '向量检索'}</dt>
          <dd>知识库类型</dd>
        </div>
        <div>
          <dt>{base.documentCount}</dt>
          <dd>文档数量</dd>
        </div>
        <div>
          <dt>{base.chunkCount}</dt>
          <dd>切片数量</dd>
        </div>
      </dl>
      <footer className="kb-card-foot">
        <span>更新于 {formatDateTime(base.updatedAt)}</span>
        {base.visibility === 'tenant' && <Badge tone="violet">租户可见</Badge>}
      </footer>
    </article>
  );
}

type EditorState = { mode: 'create' } | { mode: 'edit'; base: KnowledgeBase } | null;

export function KnowledgeBaseView({
  bases,
  loading,
  error,
  canCreate,
  canWrite,
  onChanged,
  onOpen,
}: {
  bases: KnowledgeBase[];
  loading: boolean;
  error: string | null;
  canCreate: boolean;
  canWrite: (base: KnowledgeBase) => boolean;
  onChanged: () => Promise<void>;
  onOpen: (base: KnowledgeBase) => void;
}) {
  const [search, setSearch] = useState('');
  const [editor, setEditor] = useState<EditorState>(null);
  const [removing, setRemoving] = useState<KnowledgeBase | null>(null);
  const [removeAck, setRemoveAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const filtered = bases.filter((base) =>
    (base.name ?? '').toLowerCase().includes(search.trim().toLowerCase()),
  );

  const submitEditor = async (input: { name: string; description: string; visibility: 'private' | 'tenant' }) => {
    setBusy(true);
    setFormError(null);
    try {
      if (editor?.mode === 'edit') {
        await updateKnowledgeBase(editor.base.id, {
          name: input.name,
          description: input.description || null,
          ...(canWrite(editor.base) ? { visibility: input.visibility } : {}),
        });
      } else {
        await createKnowledgeBase({
          name: input.name,
          description: input.description || undefined,
          visibility: input.visibility,
        });
      }
      setEditor(null);
      await onChanged();
    } catch (submitError) {
      setFormError(submitError instanceof Error ? submitError.message : '保存失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  const confirmRemove = async () => {
    if (!removing) return;
    setBusy(true);
    try {
      await deleteKnowledgeBase(removing.id);
      setRemoving(null);
      setRemoveAck(false);
      await onChanged();
    } catch {
      setFormError('删除失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="kc-view">
      <div className="kc-toolbar">
        {canCreate && (
          <button type="button" className="kc-button kc-button-primary" onClick={() => { setFormError(null); setEditor({ mode: 'create' }); }}>
            <PlusIcon /> 创建知识库
          </button>
        )}
        <span className="kc-count">共 {bases.length} 个知识库</span>
        <div className="kc-search">
          <SearchIcon />
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索知识库名称"
            aria-label="搜索知识库名称"
          />
        </div>
      </div>

      {formError && <p className="kc-alert" role="alert">{formError}</p>}
      {error && <p className="kc-alert" role="alert">{error}</p>}

      {loading ? (
        <div className="kc-loading"><Spinner /> 正在加载知识库…</div>
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<DatabaseIcon />}
          title={search ? '没有匹配的知识库' : bases.length === 0 ? '还没有知识库' : '没有可展示的知识库'}
          hint={bases.length === 0 && canCreate ? '点击右上角「创建知识库」开始沉淀可检索资料。' : undefined}
        />
      ) : (
        <div className="kb-grid">
          {filtered.map((base) => (
            <KnowledgeBaseCard
              key={base.id}
              base={base}
              canWrite={canWrite(base)}
              onOpen={() => onOpen(base)}
              onEdit={() => { setFormError(null); setEditor({ mode: 'edit', base }); }}
              onDelete={() => { setFormError(null); setRemoving(base); setRemoveAck(false); }}
            />
          ))}
        </div>
      )}

      {editor && (
        <KnowledgeBaseEditor
          mode={editor.mode}
          initial={editor.mode === 'edit' ? editor.base : undefined}
          visibilityLocked={editor.mode === 'edit' && !canWrite(editor.base)}
          busy={busy}
          error={formError}
          onCancel={() => setEditor(null)}
          onSubmit={submitEditor}
        />
      )}

      {removing && (
        <Modal
          title="删除知识库"
          onClose={() => setRemoving(null)}
          footer={
            <>
              <button type="button" className="kc-button" onClick={() => setRemoving(null)} disabled={busy}>取消</button>
              <button type="button" className="kc-button kc-button-danger" onClick={() => void confirmRemove()} disabled={busy || !removeAck}>
                {busy ? '正在删除…' : '确认删除'}
              </button>
            </>
          }
        >
          <p className="kc-confirm-text">
            即将删除知识库 <strong>{removing.name}</strong>，该库下的全部文档、切片与向量索引都会一并删除，且无法恢复。
          </p>
          <label className="kc-confirm-check">
            <input type="checkbox" checked={removeAck} onChange={(event) => setRemoveAck(event.target.checked)} />
            我已知晓该操作不可恢复
          </label>
        </Modal>
      )}
    </div>
  );
}

function KnowledgeBaseEditor({
  mode,
  initial,
  visibilityLocked,
  busy,
  error,
  onCancel,
  onSubmit,
}: {
  mode: 'create' | 'edit';
  initial?: KnowledgeBase;
  visibilityLocked: boolean;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (input: { name: string; description: string; visibility: 'private' | 'tenant' }) => void;
}) {
  const [name, setName] = useState(initial?.name ?? '');
  const [description, setDescription] = useState(initial?.description ?? '');
  const [visibility, setVisibility] = useState<'private' | 'tenant'>(initial?.visibility ?? 'private');

  return (
    <Modal
      title={mode === 'create' ? '创建知识库' : '编辑知识库'}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="kc-button" onClick={onCancel} disabled={busy}>取消</button>
          <button
            type="button"
            className="kc-button kc-button-primary"
            disabled={busy || !name.trim()}
            onClick={() => void onSubmit({ name: name.trim(), description: description.trim(), visibility })}
          >
            {busy ? '保存中…' : mode === 'create' ? '创建' : '保存'}
          </button>
        </>
      }
    >
      <div className="kc-form">
        <label className="kc-form-label" htmlFor="kb-name">知识库名称</label>
        <input
          id="kb-name"
          className="kc-text-input"
          value={name}
          maxLength={200}
          onChange={(event) => setName(event.target.value)}
          placeholder="例如：学生成绩知识库"
          autoFocus
        />
        <label className="kc-form-label" htmlFor="kb-desc">知识库描述</label>
        <textarea
          id="kb-desc"
          className="kc-textarea"
          value={description}
          maxLength={2000}
          rows={3}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="按业务场景组织表格知识，沉淀可检索资料"
        />
        <label className="kc-form-label">可见范围</label>
        <div className="kc-radio-group">
          <label className={`kc-radio${visibility === 'private' ? ' is-active' : ''}`}>
            <input
              type="radio"
              name="kb-visibility"
              checked={visibility === 'private'}
              disabled={visibilityLocked}
              onChange={() => setVisibility('private')}
            />
            <span>仅自己可见</span>
          </label>
          <label className={`kc-radio${visibility === 'tenant' ? ' is-active' : ''}`}>
            <input
              type="radio"
              name="kb-visibility"
              checked={visibility === 'tenant'}
              disabled={visibilityLocked}
              onChange={() => setVisibility('tenant')}
            />
            <span>租户内可见</span>
          </label>
        </div>
        {visibilityLocked && <p className="kc-form-hint">仅知识库所有者或管理员可以修改可见范围。</p>}
        {error && <p className="kc-alert" role="alert">{error}</p>}
      </div>
    </Modal>
  );
}
