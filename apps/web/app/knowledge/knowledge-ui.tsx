'use client';

import { useEffect, type ReactNode } from 'react';

import { getKnowledgeAssetContentUrl, type KnowledgeCitationImage } from './knowledge-api';

type IconProps = { className?: string };

function svg(path: ReactNode, viewBox = '0 0 24 24') {
  return function Icon({ className }: IconProps) {
    return (
      <svg className={className} viewBox={viewBox} width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {path}
      </svg>
    );
  };
}

export const DatabaseIcon = svg(<><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>);
export const DocIcon = svg(<><path d="M14 3v5h5" /><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M8 13h8M8 17h6" /></>);
export const LayersIcon = svg(<><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 13 9 5 9-5" /></>);
export const SearchIcon = svg(<><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></>);
export const ChatIcon = svg(<><path d="M21 12a8 8 0 0 1-11.5 7.2L4 21l1.8-5.5A8 8 0 1 1 21 12Z" /></>);
export const PlusIcon = svg(<><path d="M12 5v14M5 12h14" /></>);
export const UploadIcon = svg(<><path d="M12 16V4" /><path d="m7 9 5-5 5 5" /><path d="M5 16v3a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3" /></>);
export const GridIcon = svg(<><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>);
export const ListIcon = svg(<><path d="M8 6h13M8 12h13M8 18h13" /><path d="M3 6h.01M3 12h.01M3 18h.01" /></>);
export const EditIcon = svg(<><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" /></>);
export const TrashIcon = svg(<><path d="M3 6h18" /><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /><path d="M10 11v6M14 11v6" /></>);
export const SendIcon = svg(<><path d="m22 2-7 20-4-9-9-4Z" /><path d="M22 2 11 13" /></>);
export const HistoryIcon = svg(<><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" /><path d="M12 7v5l3 2" /></>);
export const CloseIcon = svg(<><path d="M18 6 6 18M6 6l12 12" /></>);
export const ChevronDownIcon = svg(<><path d="m6 9 6 6 6-6" /></>);
export const SlidersIcon = svg(<><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3" /><path d="M1 14h6M9 8h6M17 16h6" /></>);
export const QuoteIcon = svg(<><path d="M10 7H6a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h2v2a2 2 0 0 1-2 2" /><path d="M20 7h-4a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h2v2a2 2 0 0 1-2 2" /></>);
export const ArrowLeftIcon = svg(<><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></>);
export const FolderIcon = svg(<><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" /></>);
export const ImageIcon = svg(<><rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="9" cy="9" r="2" /><path d="m21 15-5-5L5 21" /></>);

export function Spinner({ className }: IconProps) {
  return <span className={`kc-spinner${className ? ` ${className}` : ''}`} aria-label="加载中" />;
}

export function Badge({ tone = 'neutral', children }: { tone?: 'green' | 'red' | 'amber' | 'neutral' | 'violet'; children: ReactNode }) {
  return <span className={`kc-badge kc-badge-${tone}`}>{children}</span>;
}

export function EmptyState({ icon, title, hint }: { icon?: ReactNode; title: string; hint?: string }) {
  return (
    <div className="kc-empty">
      {icon && <div className="kc-empty-icon">{icon}</div>}
      <p className="kc-empty-title">{title}</p>
      {hint && <p className="kc-empty-hint">{hint}</p>}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  footer,
  width = 480,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="kc-modal-mask" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="kc-modal" style={{ width }} role="dialog" aria-modal="true" aria-label={title}>
        <div className="kc-modal-header">
          <h3>{title}</h3>
          <button type="button" className="kc-icon-button" onClick={onClose} aria-label="关闭">
            <CloseIcon />
          </button>
        </div>
        <div className="kc-modal-body">{children}</div>
        {footer && <div className="kc-modal-footer">{footer}</div>}
      </div>
    </div>
  );
}

export type RetrievalParamsValue = {
  topK: number;
  minScore: number;
};

/** 检索/问答共用的左侧参数面板。重排与 Dense Weight 后端暂不支持，仅灰显。 */
export function RetrievalParamsPanel({
  value,
  onChange,
}: {
  value: RetrievalParamsValue;
  onChange: (next: RetrievalParamsValue) => void;
}) {
  return (
    <section className="kc-panel">
      <header className="kc-panel-head">
        <SlidersIcon className="kc-panel-icon" />
        <div>
          <h3>检索参数</h3>
          <p>调整检索参数，预览知识库命中效果</p>
        </div>
      </header>

      <div className="kc-field">
        <div className="kc-field-row">
          <label htmlFor="kc-topk">结果返回数量</label>
          <input
            id="kc-topk"
            className="kc-number-input"
            type="number"
            min={1}
            max={20}
            value={value.topK}
            onChange={(event) => {
              const next = Math.min(20, Math.max(1, Number(event.target.value) || 1));
              onChange({ ...value, topK: next });
            }}
          />
        </div>
        <input
          aria-label="结果返回数量滑杆"
          type="range"
          min={1}
          max={20}
          step={1}
          value={value.topK}
          onChange={(event) => onChange({ ...value, topK: Number(event.target.value) })}
        />
      </div>

      <div className="kc-field">
        <div className="kc-field-row">
          <label htmlFor="kc-score">最低相似度</label>
          <input
            id="kc-score"
            className="kc-number-input"
            type="number"
            min={0}
            max={1}
            step={0.01}
            value={value.minScore.toFixed(2)}
            onChange={(event) => {
              const next = Math.min(1, Math.max(0, Number(event.target.value) || 0));
              onChange({ ...value, minScore: next });
            }}
          />
        </div>
        <input
          aria-label="最低相似度滑杆"
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={value.minScore}
          onChange={(event) => onChange({ ...value, minScore: Number(event.target.value) })}
        />
      </div>

      <div className="kc-field kc-field-disabled" title="当前部署暂未启用重排模型">
        <div className="kc-field-row">
          <span>重排模型</span>
          <span className="kc-switch is-off" aria-disabled="true">
            <span className="kc-switch-thumb" />
          </span>
        </div>
      </div>

      <div className="kc-field kc-field-disabled" title="当前部署暂未提供稀疏向量权重">
        <div className="kc-field-row">
          <span>Dense Weight</span>
        </div>
        <input className="kc-text-input" value="0.50" disabled readOnly aria-label="Dense Weight" />
      </div>
    </section>
  );
}

/** 命中切片关联的图片缩略图列表。点击放大查看大图。 */
export function CitationImages({ kbId, images }: { kbId: string; images: KnowledgeCitationImage[] }) {
  if (!images?.length) return null;
  return (
    <div className="kc-citation-images" aria-label="切片关联图片">
      <span className="kc-citation-images-label">
        <ImageIcon /> 关联图片 {images.length}
      </span>
      <div className="kc-citation-images-grid">
        {images.map((image) => (
          <a
            key={image.assetId}
            className="kc-citation-image"
            href={getKnowledgeAssetContentUrl(kbId, image.assetId)}
            target="_blank"
            rel="noopener noreferrer"
            title={image.alt || image.name}
          >
            <img src={getKnowledgeAssetContentUrl(kbId, image.assetId)} alt={image.alt || image.name} loading="lazy" />
          </a>
        ))}
      </div>
    </div>
  );
}
